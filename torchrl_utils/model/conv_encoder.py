from typing import Sequence

import torch
from torch import nn
from torchrl.modules.models.utils import SquashDims

from torchrl_utils.model.impala_net import _ConvNetBlock


class ConvEncoder(nn.Module):
    def __init__(self,
                 raster_shape: Sequence[int],
                 cnn_channels: Sequence[int] = (16, 32, 64),
                 kernel_sizes: Sequence[int] = (8, 4, 3),
                 strides: Sequence[int] = (1, 1, 1),
                 vec_dim=14,
                 vec_out=256,
                 cnn_activation_class=torch.nn.ELU,
                 mlp_activation_class=torch.nn.ReLU,
                 ):
        super(ConvEncoder, self).__init__()
        in_ch = raster_shape[0]
        layers = []
        for i in range(len(cnn_channels)):
            layers += [_ConvNetBlock(
                in_ch, cnn_channels[i], kernel_size=kernel_sizes[i], stride=strides[i],
                activation_function=cnn_activation_class,
            )]
            # layers += [nn.BatchNorm2d(cnn_channels[i])]
            in_ch = cnn_channels[i]
        layers += [mlp_activation_class(inplace=True), SquashDims()]
        self.cnn_encoder = torch.nn.Sequential(*layers)
        dummy_inputs = torch.ones(raster_shape)
        if dummy_inputs.ndim < 4:
            dummy_inputs = dummy_inputs.unsqueeze(0)
        cnn_output = self.cnn_encoder(dummy_inputs)
        self.post_encoder = nn.Sequential(
            nn.Linear(vec_dim + cnn_output.size(1), vec_out),
            mlp_activation_class(),
            # nn.Linear(vec_out, vec_out),
            # activation_class(),
        )

    def forward(self, observation: torch.Tensor, vector=None):
        # if observation.ndim > 4:
        #     ori_shape = observation.shape
        #     observation = observation.reshape(-1, ori_shape[-3], ori_shape[-2], ori_shape[-1])
        #     embed = self.cnn_encoder(observation)
        #     embed = embed.reshape(list(ori_shape[:-3]) + [-1])
        # else:
        embed = self.cnn_encoder(observation)
        if vector is not None:
            embed = torch.concatenate([
                embed,
                vector,
            ], dim=-1)
        embed = self.post_encoder(embed)
        return embed
