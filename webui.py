# -*- coding: utf-8 -*-
import os
import math
import datetime
from functools import partial

import numpy as np
import torch
from PIL import Image
from omegaconf import OmegaConf
from transformers import AutoTokenizer, Wav2Vec2Model, Wav2Vec2Processor
from moviepy.editor import VideoFileClip, AudioFileClip
import librosa

# Custom modules
from diffusers import FlowMatchEulerDiscreteScheduler

from src.dist import set_multi_gpus_devices
from src.wan_vae import AutoencoderKLWan
from src.wan_image_encoder import  CLIPModel
from src.wan_text_encoder import  WanT5EncoderModel
from src.wan_transformer3d_audio import WanTransformerAudioMask3DModel
from src.pipeline_wan_fun_inpaint_audio import WanFunInpaintAudioPipeline

from src.utils import (
    filter_kwargs,
    get_image_to_video_latent3,
    save_videos_grid,
)
from src.fm_solvers import FlowDPMSolverMultistepScheduler
from src.fm_solvers_unipc import FlowUniPCMultistepScheduler
from src.cache_utils import get_teacache_coefficients

from src.face_detect import get_mask_coord

from mmgp import offload, profile_type
import argparse
import gradio as gr
import random
import gc


parser = argparse.ArgumentParser()
parser.add_argument("--server_name", type=str, default="127.0.0.1")
parser.add_argument("--server_port", type=int, default=7860)
parser.add_argument("--share", action="store_true")
parser.add_argument("--mcp_server", action="store_true")
parser.add_argument("--max_vram", type=float, default=0.9)
parser.add_argument("--compile", action="store_true")
args = parser.parse_args()


if torch.cuda.is_available():
    device = "cuda"
    if torch.cuda.get_device_capability()[0] >= 8:
        dtype = torch.bfloat16
    else:
        dtype = torch.float16
else:
    device = "cpu"


class Config:
    def __init__(self):
        self.ulysses_degree = 1
        self.ring_degree = 1
        self.fsdp_dit = False

        self.num_skip_start_steps = 5
        self.teacache_offload = True
        self.cfg_skip_ratio = 0
        self.enable_riflex = False
        self.riflex_k = 6

        self.config_path = "config/config.yaml"
        self.model_name = "models/Wan2.1-Fun-V1.1-1.3B-InP"
        self.transformer_path = "models/transformer/diffusion_pytorch_model.safetensors"

        self.sampler_name = "Flow_DPM++"
        self.audio_scale = 1.0
        self.enable_teacache = True
        self.teacache_threshold = 0.1
        self.shift = 5.0
        self.use_un_ip_mask = False

        self.overlap_video_length = 8
        self.neg_scale = 1.5
        self.neg_steps = 2
        self.guidance_scale = 4.5
        self.audio_guidance_scale = 2.5
        self.use_dynamic_cfg = True
        self.use_dynamic_acfg = True
        self.num_inference_steps = 20
        self.lora_weight = 1.0

        self.weight_dtype = torch.bfloat16
        self.sample_size = [768, 768]
        self.fps = 25

        self.wav2vec_model_dir = "models/wav2vec2-base-960h"


def load_wav2vec_models(wav2vec_model_dir):
    processor = Wav2Vec2Processor.from_pretrained(wav2vec_model_dir)
    model = Wav2Vec2Model.from_pretrained(wav2vec_model_dir).eval().to("cuda")
    model.requires_grad_(False)
    return processor, model


def extract_audio_features(audio_path, processor, model):
    sr = 16000
    audio_segment, sample_rate = librosa.load(audio_path, sr=sr)
    input_values = processor(audio_segment, sampling_rate=sample_rate, return_tensors="pt").input_values
    input_values = input_values.to(model.device)
    features = model(input_values).last_hidden_state
    return features.squeeze(0)


def get_sample_size(image, default_size):
    width, height = image.size
    original_area = width * height
    default_area = default_size[0] * default_size[1]

    if default_area < original_area:
        ratio = math.sqrt(original_area / default_area)
        width = width / ratio // 16 * 16
        height = height / ratio // 16 * 16
    else:
        width = width // 16 * 16
        height = height // 16 * 16

    return int(height), int(width)


def get_ip_mask(coords):
    y1, y2, x1, x2, h, w = coords
    Y, X = torch.meshgrid(torch.arange(h), torch.arange(w), indexing='ij')
    mask = (Y.unsqueeze(-1) >= y1) & (Y.unsqueeze(-1) < y2) & (X.unsqueeze(-1) >= x1) & (X.unsqueeze(-1) < x2)
    return mask.reshape(-1).float()


config = Config()

device = set_multi_gpus_devices(config.ulysses_degree, config.ring_degree)

cfg = OmegaConf.load(config.config_path)

transformer = WanTransformerAudioMask3DModel.from_pretrained(
    os.path.join(config.model_name, cfg['transformer_additional_kwargs'].get('transformer_subpath', 'transformer')),
    transformer_additional_kwargs=OmegaConf.to_container(cfg['transformer_additional_kwargs']),
    torch_dtype=config.weight_dtype,
)
if config.transformer_path is not None:
    if config.transformer_path.endswith("safetensors"):
        from safetensors.torch import load_file
        state_dict = load_file(config.transformer_path)
    else:
        state_dict = torch.load(config.transformer_path)
    state_dict = state_dict["state_dict"] if "state_dict" in state_dict else state_dict
    transformer.load_state_dict(state_dict, strict=False)

vae = AutoencoderKLWan.from_pretrained(
    os.path.join(config.model_name, cfg['vae_kwargs'].get('vae_subpath', 'vae')),
    additional_kwargs=OmegaConf.to_container(cfg['vae_kwargs']),
).to(config.weight_dtype)

tokenizer = AutoTokenizer.from_pretrained(
    os.path.join(config.model_name, cfg['text_encoder_kwargs'].get('tokenizer_subpath', 'tokenizer')),
)

text_encoder = WanT5EncoderModel.from_pretrained(
    os.path.join(config.model_name, cfg['text_encoder_kwargs'].get('text_encoder_subpath', 'text_encoder')),
    additional_kwargs=OmegaConf.to_container(cfg['text_encoder_kwargs']),
    torch_dtype=config.weight_dtype,
).eval()

clip_image_encoder = CLIPModel.from_pretrained(
    os.path.join(config.model_name, cfg['image_encoder_kwargs'].get('image_encoder_subpath', 'image_encoder')),
).to(config.weight_dtype).eval()

scheduler_cls = {
    "Flow": FlowMatchEulerDiscreteScheduler,
    "Flow_Unipc": FlowUniPCMultistepScheduler,
    "Flow_DPM++": FlowDPMSolverMultistepScheduler,
}[config.sampler_name]
scheduler = scheduler_cls(**filter_kwargs(scheduler_cls, OmegaConf.to_container(cfg['scheduler_kwargs'])))

pipeline = WanFunInpaintAudioPipeline(
    transformer=transformer,
    vae=vae,
    tokenizer=tokenizer,
    text_encoder=text_encoder,
    scheduler=scheduler,
    clip_image_encoder=clip_image_encoder,
)

budgets = int(torch.cuda.get_device_properties(0).total_memory/1048576 * args.max_vram)
offload.profile(pipeline, profile_type.LowRAM_HighVRAM,
                budgets={'*': budgets}, compile=True if args.compile else False)

if config.enable_teacache:
    coefficients = get_teacache_coefficients(config.model_name)
    pipeline.transformer.enable_teacache(coefficients, config.num_inference_steps, config.teacache_threshold,
                                         num_skip_start_steps=config.num_skip_start_steps, offload=config.teacache_offload)

# 加载 wav2vec2 用于提取音频特征
wav2vec_processor, wav2vec_model = load_wav2vec_models(config.wav2vec_model_dir)


def generate(
    image,
    audio,
    prompt,
    negative_prompt,
    partial_video_length,
    guidance_scale,
    audio_guidance_scale,
    seed_param
):
    if seed_param < 0:
        seed = random.randint(0, np.iinfo(np.int32).max)
    else:
        seed = int(seed_param)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = "outputs"
    os.makedirs(save_path, exist_ok=True)

    generator = torch.Generator(device=device).manual_seed(seed)

    # 读取参考图片和检测mask
    ref_img = Image.open(image).convert("RGB")
    y1, y2, x1, x2, h_, w_ = get_mask_coord(image)

    # 提取音频特征
    audio_clip = AudioFileClip(audio)
    audio_features = extract_audio_features(audio, wav2vec_processor, wav2vec_model)
    audio_embeds = audio_features.unsqueeze(0).to(device=device, dtype=config.weight_dtype)

    # 计算总视频帧
    video_length = int(audio_clip.duration * config.fps)
    video_length = (
        int((video_length - 1) // vae.config.temporal_compression_ratio *
            vae.config.temporal_compression_ratio) + 1 if video_length != 1 else 1
    )

    if config.enable_riflex:
        latent_frames_total = (video_length - 1) // vae.config.temporal_compression_ratio + 1
        pipeline.transformer.enable_riflex(k=config.riflex_k, L_test=latent_frames_total)

    # 获取采样尺寸
    sample_height, sample_width = get_sample_size(ref_img, config.sample_size)
    downratio = math.sqrt(sample_height * sample_width / h_ / w_)
    coords = (
        y1 * downratio // 16, y2 * downratio // 16,
        x1 * downratio // 16, x2 * downratio // 16,
        sample_height // 16, sample_width // 16,
    )
    ip_mask = get_ip_mask(coords).unsqueeze(0)
    ip_mask = torch.cat([ip_mask] * 3).to(device=device, dtype=config.weight_dtype)

    # clip 图像
    _, _, clip_image = get_image_to_video_latent3(ref_img, None, video_length=partial_video_length,
                                                  sample_size=[sample_height, sample_width])

    init_frames = 0
    last_frames = init_frames + partial_video_length
    new_sample = None
    prompt_embeds, negative_prompt_embeds = pipeline.encode_prompt(prompt, negative_prompt, dtype=dtype)

    # ============== 循环分段生成，确保音频长度对齐 ==============
    while init_frames < video_length:
        # 如果剩余长度不足 partial_video_length 就截短
        if last_frames >= video_length:
            partial_video_length = video_length - init_frames
            partial_video_length = max(1, int((partial_video_length - 1) //
                                              vae.config.temporal_compression_ratio *
                                              vae.config.temporal_compression_ratio) + 1)
        latent_frames = (partial_video_length - 1) // vae.config.temporal_compression_ratio + 1

        input_video, input_video_mask, _ = get_image_to_video_latent3(ref_img, None,
                                                                      video_length=partial_video_length,
                                                                      sample_size=[sample_height, sample_width])

        # 精确裁切音频嵌入
        needed_audio_tokens = partial_video_length * 2  # 每帧对应两个音频token
        start_tok = init_frames * 2
        end_tok = start_tok + needed_audio_tokens
        if end_tok > audio_embeds.shape[1]:  # 边界填充零防错位
            pad_len = end_tok - audio_embeds.shape[1]
            pad_tensor = torch.zeros((1, pad_len, audio_embeds.shape[2]),
                                     device=audio_embeds.device, dtype=audio_embeds.dtype)
            partial_audio_embeds = torch.cat([audio_embeds[:, start_tok:], pad_tensor], dim=1)
        else:
            partial_audio_embeds = audio_embeds[:, start_tok:end_tok]

        # 调用pipeline生成
        sample = pipeline(
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
            num_frames=partial_video_length,
            audio_embeds=partial_audio_embeds,
            audio_scale=config.audio_scale,
            ip_mask=ip_mask,
            use_un_ip_mask=config.use_un_ip_mask,
            height=sample_height,
            width=sample_width,
            generator=generator,
            neg_scale=config.neg_scale,
            neg_steps=config.neg_steps,
            use_dynamic_cfg=config.use_dynamic_cfg,
            use_dynamic_acfg=config.use_dynamic_acfg,
            guidance_scale=guidance_scale,
            audio_guidance_scale=audio_guidance_scale,
            num_inference_steps=config.num_inference_steps,
            video=input_video,
            mask_video=input_video_mask,
            clip_image=clip_image,
            cfg_skip_ratio=config.cfg_skip_ratio,
            shift=config.shift,
        ).videos

        # 拼接前后片段
        if init_frames != 0:
            mix_ratio = torch.linspace(0., 1., config.overlap_video_length, dtype=sample.dtype,
                                       device=sample.device).view(1, 1, -1, 1, 1)
            new_sample[:, :, -config.overlap_video_length:] = \
                new_sample[:, :, -config.overlap_video_length:] * (1 - mix_ratio) + \
                sample[:, :, :config.overlap_video_length] * mix_ratio
            new_sample = torch.cat([new_sample, sample[:, :, config.overlap_video_length:]], dim=2)
            sample = new_sample
        else:
            new_sample = sample

        if last_frames >= video_length:
            break

        ref_img = [
            Image.fromarray((sample[0, :, i].permute(1, 2, 0) * 255).cpu().numpy().astype(np.uint8))
            for i in range(-config.overlap_video_length, 0)
        ]
        init_frames += partial_video_length - config.overlap_video_length
        last_frames = init_frames + partial_video_length

    # 保存视频文件
    video_path = os.path.join(save_path, f"{timestamp}.mp4")
    video_audio_path = os.path.join(save_path, f"{timestamp}_audio.mp4")
    save_videos_grid(sample[:, :, :sample.shape[2]], video_path, fps=config.fps)

    video_clip = VideoFileClip(video_path)
    audio_clip = audio_clip.subclip(0, sample.shape[2] / config.fps)
    video_clip = video_clip.set_audio(audio_clip)
    video_clip.write_videofile(video_audio_path, codec="libx264", audio_codec="aac", threads=2)

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()

    return video_audio_path, seed


with gr.Blocks(theme=gr.themes.Base()) as demo:
    gr.Markdown("<h2 style='text-align:center'>EchoMimicV3 4090d量化版 构建by小果</h2>")
    with gr.TabItem("EchoMimicV3"):
        with gr.Row():
            with gr.Column():
                image = gr.Image(label="上传图片", type="filepath", height=400)
                audio = gr.Audio(label="上传音频", type="filepath")
                prompt = gr.Textbox(label="提示词", value="")
                negative_prompt = gr.Textbox(label="负面提示词", 
                                             value="Gesture is bad. Gesture is unclear.")
                partial_video_length = gr.Slider(label="分段长度",
                                                 minimum=49, maximum=161, step=16, value=113)
                guidance_scale = gr.Slider(label="guidance scale",
                                           minimum=1.0, maximum=10.0, step=0.1, value=4.5)
                audio_guidance_scale = gr.Slider(label="audio guidance scale",
                                                 minimum=1.0, maximum=10.0, step=0.1, value=2.5)
                seed_param = gr.Number(label="种子", value=43)
                generate_button = gr.Button("🎬 开始生成", variant='primary')
            with gr.Column():
                video_output = gr.Video(label="生成结果", interactive=False)
                seed_output = gr.Textbox(label="种子")

        gr.on(triggers=[generate_button.click],
              fn=generate,
              inputs=[image, audio, prompt, negative_prompt,
                      partial_video_length, guidance_scale,
                      audio_guidance_scale, seed_param],
              outputs=[video_output, seed_output])

if __name__ == "__main__":
    demo.launch(server_name=args.server_name,
                server_port=args.server_port,
                share=args.share,
                mcp_server=args.mcp_server,
                inbrowser=True)
