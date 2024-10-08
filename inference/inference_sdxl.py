import os
import json
import torch
import hydra
from omegaconf import OmegaConf
from diffusers import AutoencoderKL
from ..utils import draw_box, setup_logger
from transformers import CLIPTextModel, CLIPTokenizer
from ..my_model.sdxl.sdxl import StableDiffusionXLPipeline
from ..my_model.sdxl.unet_2d_condition_xl import UNet2DConditionModel


@hydra.main(version_base=None, config_path="../conf", config_name="base_config")
def main(cfg):
    # build and load model
    with open(cfg.general.unet_config) as f:
        unet_config = json.load(f)

    print('inference中main初始化')

    tokenizer = CLIPTokenizer.from_pretrained(cfg.general.model_path, subfolder="tokenizer", local_files_only=True)
    text_encoder = CLIPTextModel.from_pretrained(cfg.general.model_path, subfolder="text_encoder", local_files_only=True)
    vae = AutoencoderKL.from_pretrained(cfg.general.model_path, subfolder="vae", local_files_only=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    vae.to(device)
    text_encoder.to(device)

    # ------------------ 示例输入 ------------------
    examples = {"prompt": "A hello kitty toy is playing with a purple ball.",
                "phrases": "hello kitty; ball",
                "bboxes": [[[0.1, 0.2, 0.5, 0.8]], [[0.75, 0.6, 0.95, 0.8]]],
                'save_path': cfg.general.save_path
                }

    # ------------------ 真实图像编辑示例输入 ------------------
    if cfg.general.real_image_editing:
        examples = {"prompt": "A {} is standing on grass.".format(cfg.real_image_editing.placeholder_token),
                    "phrases": "{}".format(cfg.real_image_editing.placeholder_token),
                    "bboxes": [[[0.4, 0.2, 0.9, 0.9]]],
                    'save_path': cfg.general.save_path
                    }
    # ---------------------------------------------------

    # 准备保存路径
    if not os.path.exists(cfg.general.save_path):
        os.makedirs(cfg.general.save_path)
    logger = setup_logger(cfg.general.save_path, __name__)

    logger.info(cfg)
    # Save cfg
    logger.info("save config to {}".format(os.path.join(cfg.general.save_path, 'config.yaml')))
    OmegaConf.save(cfg, os.path.join(cfg.general.save_path, 'config.yaml'))

    pipe = StableDiffusionXLPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16, variant="fp16",
        use_safetensors=True, local_files_only=True, device_map="auto"
    )
    pipe.unet = UNet2DConditionModel(**unet_config).from_pretrained(cfg.general.model_path, subfolder="unet")

    pipe.to('cuda:0')

    # 推理
    pil_images = pipe(
        prompt=examples['prompt'],
        vae=vae,
        tokenizer=tokenizer,
        text_encoder=text_encoder,
        bboxes=examples['bboxes'],
        phrases=examples['phrases'],
        cfg=cfg,
        logger=logger
    )

    # 保存示例图片
    for index, pil_image in enumerate(pil_images):
        image_path = os.path.join(cfg.general.save_path, 'example_{}.png'.format(index))
        logger.info('save example image to {}'.format(image_path))
        draw_box(pil_image, examples['bboxes'], examples['phrases'], image_path)


if __name__ == "__main__":
    main()
