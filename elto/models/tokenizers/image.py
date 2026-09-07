from dataclasses import dataclass
import os
import sys

import torch
import torch.nn as nn
from einops import rearrange
from transformers.models.vit.modeling_vit import ViTModel

from ...utils import BaseModule


def _local_dino_config_path() -> str:
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "dino_vitb16_config.json")


class DINOSingleImageTokenizer(BaseModule):
    @dataclass
    class Config(BaseModule.Config):
        enable_gradient_checkpointing: bool = False

    cfg: Config

    def configure(self) -> None:
        config_path = _local_dino_config_path()
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"find {config_path}")

        self.model: ViTModel = ViTModel(
            ViTModel.config_class.from_pretrained(config_path)
        )

        if self.cfg.enable_gradient_checkpointing:
            self.model.encoder.gradient_checkpointing = True

        self.register_buffer(
            "image_mean",
            torch.as_tensor([0.485, 0.456, 0.406]).reshape(1, 1, 3, 1, 1),
            persistent=False,
        )
        self.register_buffer(
            "image_std",
            torch.as_tensor([0.229, 0.224, 0.225]).reshape(1, 1, 3, 1, 1),
            persistent=False,
        )

    def forward(self, images: torch.FloatTensor, **kwargs) -> torch.FloatTensor:
        packed = False
        if images.ndim == 4:
            packed = True
            images = images.unsqueeze(1)

        batch_size, n_input_views = images.shape[:2]
        images = (images - self.image_mean) / self.image_std
        out = self.model(
            rearrange(images, "B N C H W -> (B N) C H W"), interpolate_pos_encoding=True
        )
        local_features, global_features = out.last_hidden_state, out.pooler_output
        local_features = local_features.permute(0, 2, 1)
        local_features = rearrange(
            local_features, "(B N) Ct Nt -> B N Ct Nt", B=batch_size
        )
        if packed:
            local_features = local_features.squeeze(1)

        return local_features

    def detokenize(self, *args, **kwargs):
        raise NotImplementedError