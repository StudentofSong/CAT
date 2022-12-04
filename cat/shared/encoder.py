# Copyright 2022 Tsinghua University
# Apache 2.0.
# Author: Keyu An,
#         Huahuan Zheng (maxwellzh@outlook.com)

"""Decoder modules impl
"""
from torchaudio.models.wav2vec2.utils import import_huggingface_model
from transformers import Wav2Vec2ForCTC
from . import layer as c_layers

import numpy as np
from collections import OrderedDict
from typing import Literal

import math
import torch
import torch.nn as nn

import transformers
transformers.logging.set_verbosity_error()


def get_vgg2l_odim(idim, in_channel=1, out_channel=128):
    idim = idim / in_channel
    idim = np.ceil(np.array(idim, dtype=np.float32) / 2)  # 1st max pooling
    idim = np.ceil(np.array(idim, dtype=np.float32) / 2)  # 2nd max pooling
    return int(idim) * out_channel  # numer of channels


class AbsEncoder(nn.Module):
    def __init__(self, with_head: bool = True, num_classes: int = -1, n_hid: int = -1) -> None:
        super().__init__()
        if with_head:
            assert num_classes > 0, f"Vocab size should be > 0, instead {num_classes}"
            assert n_hid > 0, f"Hidden size should be > 0, instead {n_hid}"
            self.classifier = nn.Linear(n_hid, num_classes)
        else:
            self.classifier = nn.Identity()

    def impl_forward(self, *args, **kwargs):
        '''Implement the forward funcion w/o classifier'''
        raise NotImplementedError

    def forward(self, *args, **kwargs):
        out = self.impl_forward(*args, **kwargs)
        if isinstance(out, tuple):
            _co = self.classifier(out[0])
            return (_co,)+out[1:]
        else:
            return self.classifier(out)


class LSTM(AbsEncoder):
    def __init__(self,
                 idim: int,
                 hdim: int,
                 n_layers: int,
                 dropout: float,
                 num_classes: int = -1,
                 with_head: bool = True,
                 bidirectional: bool = False):
        super().__init__(with_head=with_head, num_classes=num_classes,
                         n_hid=(2*hdim if bidirectional else hdim))

        self.lstm = c_layers._LSTM(
            idim, hdim, n_layers, dropout, bidirectional=bidirectional)

    def impl_forward(self, x: torch.Tensor, ilens: torch.Tensor, hidden=None):
        return self.lstm(x, ilens, hidden)


class VGGLSTM(LSTM):
    def __init__(self,
                 idim: int,
                 hdim: int,
                 n_layers: int,
                 dropout: float,
                 num_classes: int = -1,
                 with_head: bool = True,
                 in_channel: int = 3,
                 bidirectional: int = False):
        super().__init__(get_vgg2l_odim(idim, in_channel),
                         hdim, n_layers, num_classes, dropout, with_head, bidirectional)

        self.VGG = c_layers.VGG2L(in_channel)

    def impl_forward(self, x: torch.Tensor, ilens: torch.Tensor):
        vgg_o, vgg_lens = self.VGG(x, ilens)
        return super().impl_forward(vgg_o, vgg_lens)


class LSTMrowCONV(AbsEncoder):
    def __init__(self,
                 idim: int,
                 hdim: int,
                 n_layers: int,
                 dropout: float,
                 with_head: bool = True,
                 num_classes: int = -1) -> None:
        super().__init__(with_head=with_head, num_classes=num_classes, n_hid=hdim)

        self.lstm = c_layers._LSTM(idim, hdim, n_layers, dropout)
        self.lookahead = c_layers.Lookahead(hdim, context=5)

    def impl_forward(self, x: torch.Tensor, ilens: torch.Tensor, hidden=None):
        lstm_out, olens = self.lstm(x, ilens, hidden)
        ahead_out = self.lookahead(lstm_out)
        return ahead_out, olens


class TDNN_NAS(AbsEncoder):
    def __init__(self,
                 idim: int,
                 hdim: int,
                 dropout: float = 0.5,
                 num_classes: int = -1,
                 with_head: bool = True) -> None:
        super().__init__(with_head=with_head, num_classes=num_classes, n_hid=hdim)

        self.dropout = nn.Dropout(dropout)
        self.tdnns = nn.ModuleDict(OrderedDict([
            ('tdnn0', c_layers.TDNN(idim, hdim, half_context=2, dilation=1)),
            ('tdnn1', c_layers.TDNN(hdim, hdim, half_context=2, dilation=2)),
            ('tdnn2', c_layers.TDNN(hdim, hdim, half_context=2, dilation=1)),
            ('tdnn3', c_layers.TDNN(hdim, hdim, stride=3)),
            ('tdnn4', c_layers.TDNN(hdim, hdim, half_context=2, dilation=2)),
            ('tdnn5', c_layers.TDNN(hdim, hdim, half_context=2, dilation=1)),
            ('tdnn6', c_layers.TDNN(hdim, hdim, half_context=2, dilation=2))
        ]))

    def impl_forward(self, x: torch.Tensor, ilens: torch.Tensor):
        for i in range(6):
            x = self.dropout(x)
            x, ilens = self.tdnns[f"tdnn{i}"](x, ilens)

        return self.tdnns['tdnn6'](x, ilens)


class TDNN_LSTM(AbsEncoder):
    def __init__(self,
                 idim: int,
                 hdim: int,
                 n_layers: int,
                 dropout: float,
                 num_classes: int = -1,
                 with_head: bool = True) -> None:
        super().__init__(with_head=with_head, num_classes=num_classes, n_hid=hdim)

        self.tdnn_init = c_layers.TDNN(idim, hdim)
        assert n_layers > 0
        self.n_layers = n_layers
        self.cells = nn.ModuleDict()
        for i in range(n_layers):
            self.cells[f"tdnn{i}-0"] = c_layers.TDNN(hdim, hdim)
            self.cells[f"tdnn{i}-1"] = c_layers.TDNN(hdim, hdim)
            self.cells[f"lstm{i}"] = c_layers._LSTM(hdim, hdim, 1)
            self.cells[f"bn{i}"] = c_layers.MaskedBatchNorm1d(
                hdim, eps=1e-5, affine=True)
            self.cells[f"dropout{i}"] = nn.Dropout(dropout)

    def impl_forward(self, x: torch.Tensor, ilens: torch.Tensor):
        tmpx, tmp_lens = self.tdnn_init(x, ilens)
        for i in range(self.n_layers):
            tmpx, tmp_lens = self.cells[f"tdnn{i}-0"](tmpx, tmp_lens)
            tmpx, tmp_lens = self.cells[f"tdnn{i}-1"](tmpx, tmp_lens)
            tmpx, tmp_lens = self.cells[f"lstm{i}"](tmpx, tmp_lens)
            tmpx = self.cells[f"bn{i}"](tmpx, tmp_lens)
            tmpx = self.cells[f"dropout{i}"](tmpx)

        return tmpx, tmp_lens


class BLSTMN(AbsEncoder):
    def __init__(self,
                 idim: int,
                 hdim: int,
                 n_layers: int,
                 dropout: float,
                 num_classes: int = -1,
                 with_head: bool = True) -> None:
        super().__init__(with_head=with_head, num_classes=num_classes, n_hid=hdim)

        assert n_layers > 0
        self.cells = nn.ModuleDict()
        self.n_layers = n_layers
        for i in range(n_layers):
            if i == 0:
                inputdim = idim
            else:
                inputdim = hdim * 2
            self.cells[f"lstm{i}"] = c_layers._LSTM(
                inputdim, hdim, 1, bidirectional=True)
            self.cells[f"bn{i}"] = c_layers.MaskedBatchNorm1d(
                hdim*2, eps=1e-5, affine=True)
            self.cells[f"dropout{i}"] = nn.Dropout(dropout)

    def impl_forward(self, x: torch.Tensor, ilens: torch.Tensor):
        tmp_x, tmp_lens = x, ilens
        for i in range(self.n_layers):
            tmp_x, tmp_lens = self.cells[f"lstm{i}"](tmp_x, tmp_lens)
            tmp_x = self.cells[f"bn{i}"](tmp_x, tmp_lens)
            tmp_x = self.cells[f"dropout{i}"](tmp_x)

        return tmp_x, tmp_lens


class ConformerNet(AbsEncoder):
    """The conformer model with convolution subsampling

    Args:
        num_cells (int): number of conformer blocks
        idim (int): dimension of input features
        hdim (int): hidden size in conformer blocks
        num_classes (int): number of output classes
        conv_multiplier (int): the multiplier to conv subsampling module
        dropout_in (float): the dropout rate to input of conformer blocks (after the linear and subsampling layers)
        res_factor (float): the weighted-factor of residual-connected shortcut in feed-forward module
        d_head (int): dimension of heads in multi-head attention module
        num_heads (int): number of heads in multi-head attention module
        kernel_size (int): kernel size in convolution module
        multiplier (int): multiplier of depth conv in convolution module 
        dropout (float): dropout rate to all conformer internal modules
        delta_feats (bool): True if the input features contains delta and delta-delta features; False if not.
    """

    def __init__(
            self,
            num_cells: int,
            idim: int,
            hdim: int,
            num_classes: int = -1,
            conv: Literal['conv2d', 'vgg2l', 'none'] = 'conv2d',
            conv_multiplier: int = None,
            dropout_in: float = 0.2,
            res_factor: float = 0.5,
            d_head: int = -1,
            num_heads: int = 4,
            kernel_size: int = 32,
            multiplier: int = 1,
            dropout: float = 0.1,
            dropout_attn: float = 0.0,
            delta_feats: bool = False,
            with_head: bool = True,
            subsample_norm: str = 'none',
            time_reduction_factor: int = 1,
            time_reduction_pos: int = -1):
        super().__init__(with_head=with_head, num_classes=num_classes, n_hid=hdim)
        assert isinstance(time_reduction_factor, int)
        assert time_reduction_factor >= 1

        if delta_feats:
            in_channel = 3
        else:
            in_channel = 1

        if conv == 'vgg2l':
            self.conv_subsampling = c_layers.VGG2LSubsampling(in_channel)
            ch_sub = math.ceil(math.ceil((idim//in_channel)/2)/2)
            conv_dim = 128 * ch_sub
        elif conv == 'conv2d':
            if conv_multiplier is None:
                conv_multiplier = hdim
            self.conv_subsampling = c_layers.Conv2dSubdampling(
                conv_multiplier, norm=subsample_norm, stacksup=delta_feats)
            conv_dim = conv_multiplier * (((idim//in_channel)//2)//2)
        elif conv == 'none':
            self.conv_subsampling = None
            conv_dim = idim
        else:
            raise RuntimeError(f"Unknown type of convolutional layer: {conv}")

        self.linear_drop = nn.Sequential(OrderedDict([
            ('linear', nn.Linear(conv_dim, hdim)),
            ('dropout', nn.Dropout(dropout_in))
        ]))

        self.cells = nn.ModuleList()
        pe = c_layers.PositionalEncoding(hdim)

        for i in range(num_cells):
            if i == time_reduction_pos and time_reduction_factor > 1:
                cell = c_layers.TimeReduction(time_reduction_factor)
                self.cells.append(cell)

            cell = c_layers.ConformerCell(
                hdim, pe, res_factor, d_head, num_heads, kernel_size, multiplier, dropout, dropout_attn)
            self.cells.append(cell)

        if time_reduction_factor > 1 and time_reduction_pos == -1:
            self.cells.append(c_layers.TimeReduction(time_reduction_factor))

    def impl_forward(self, x: torch.Tensor, lens: torch.Tensor):
        if self.conv_subsampling is None:
            x_subsampled, ls = x, lens
        else:
            x_subsampled, ls = self.conv_subsampling(x, lens)
        out = self.linear_drop(x_subsampled)
        for cell in self.cells:
            out, ls = cell(out, ls)

        return out, ls


class ConformerLSTM(ConformerNet):
    """Stack LSTM after conformer blocks."""

    def __init__(self,
                 hdim_lstm: int,
                 num_lstm_layers: int,
                 dropout_lstm: float,
                 bidirectional: bool = False,
                 **kwargs):
        super().__init__(**kwargs)

        self.lstm = c_layers._LSTM(
            idim=self.linear_drop.linear.out_features,
            hdim=hdim_lstm,
            n_layers=num_lstm_layers,
            dropout=dropout_lstm,
            bidirectional=bidirectional
        )
        if bidirectional:
            hdim_lstm *= 2
        self.classifier = nn.Linear(hdim_lstm, kwargs['num_classes'])

    def impl_forward(self, x: torch.Tensor, lens: torch.Tensor):
        conv_x, conv_ls = super().impl_forward(x, lens)
        return self.lstm(conv_x, conv_ls)


class Wav2Vec2Encoder(AbsEncoder):
    def __init__(
            self,
            pretrained_model: str,
            use_wav2vec2_encoder: bool = False,
            tune_wav2vec2: bool = False,
            enc_head_type: str = 'ConformerNet', **enc_head_kwargs) -> None:
        """
        pretrained_model (str) : huggingface pretrained model, e.g. facebook/wav2vec2-base
        use_wav2vec2_encoder (bool) : if True, use the pretrained wav2vec2 transformer
        tune_wav2vec2 (bool) : if True, allow wav2vec models to be updated
        enc_head_type (str): any of the AbsEncoder class, or 'Linear'
        enc_head_kwargs : options passed to enc_head_type()

        dataflow in forward:
            x -> wav2vec2_feature_extractor -> (wav2vec2_encoder) -> enc_head -> out
        """
        super().__init__(False)

        assert enc_head_type.isidentifier(), "invalid type"
        if enc_head_type == 'Linear':
            # generally, odim of _wav2vec2_feat_extractor is 512
            # ... and that of _wav2vec2_encoder is 768
            self._enc_head = nn.Linear(
                enc_head_kwargs['idim'], enc_head_kwargs['num_classes'])
        else:
            T_enc = eval(enc_head_type)
            assert issubclass(T_enc, AbsEncoder)
            self._enc_head = T_enc(**enc_head_kwargs)   # type: AbsEncoder

        wav2vec2_one = import_huggingface_model(
            Wav2Vec2ForCTC.from_pretrained(pretrained_model))
        self._wav2vec2_feat_extractor = wav2vec2_one.feature_extractor
        if use_wav2vec2_encoder:
            self._wav2vec2_encoder = wav2vec2_one.encoder
        else:
            self._wav2vec2_encoder = None

        if not tune_wav2vec2:
            self._wav2vec2_feat_extractor.requires_grad_(False)
            if use_wav2vec2_encoder:
                self._wav2vec2_encoder.requires_grad_(False)

    def forward(self, x: torch.Tensor, xlens: torch.Tensor):
        x, xlens = self._wav2vec2_feat_extractor(
            x.squeeze(2), xlens)
        if self._wav2vec2_encoder is not None:
            x = self._wav2vec2_encoder(x, xlens)
        return self._enc_head(x, xlens)
    

class JoinAPLinearEncode(AbsEncoder):
    def __init__(
        self,
        pv_path: str,
        enc_head_type: str = 'LSTM', **enc_head_kwargs) -> None:
        """
        pv_path（str）：the path of pv
        enc_head_type (str): any of the AbsEncoder class, or 'Linear'
        enc_head_kwargs : options passed to enc_head_type()
        Param:
            P: phonological vector matrix
            A: phoneme transformation matrix with size [phonological_dim, phoneme_dim]
        Please refer to Equation (2) of Sec. 3.2 in the paper.
        """
        super().__init__(False)
     
        self.A = nn.Linear(51,  enc_head_kwargs['hdim'])
        self.P, p_c = self.init_pv(pv_path)
        self.linear = nn.Linear(p_c, enc_head_kwargs['num_classes'])
        T_enc = eval(enc_head_type)
        assert issubclass(T_enc, AbsEncoder)
        self._enc_head = T_enc(**enc_head_kwargs)
    def init_pv(self, fin):
        pv = torch.Tensor(np.load(fin))  
        P = nn.Parameter(pv)
        p_c = P.size()[0]
        return P, p_c
    def forward(self, x: torch.Tensor, xlens: torch.Tensor,hidden=None):
        enc_out,ls = self.T_enc(x,xlens,hidden)
        out = self.A(self.P) # [pv_dim, hdim]
        out = torch.einsum("bth, ch -> btc", enc_out, out)
        out = self.linear(out) # [batch, time, num_class]
        return out, ls
    
    
class JoinAPNonLinearEncode(AbsEncoder):
    def __init__(
        self,
        pv_path: str,
        enc_head_type: str = 'LSTM', **enc_head_kwargs) -> None:
        """
        pv_path（str）：the path of pv
        enc_head_type (str): any of the AbsEncoder class, or 'Linear'
        enc_head_kwargs : options passed to enc_head_type()
        Params:
            P   : phonological vector matrix
            A1  : linear transformation matrix with size [phonological_dim, hdim1]
            A2  : linear transformation matrix with size [hdim1, hdim2]
        Please refer to Equation (3) in Sec. 3.2 in the paper.
        """
        super().__init__(False)
        self.A2 = nn.Linear(512, enc_head_kwargs['hdim'])
        self.A1 = nn.Linear(51, 512)
        self.sig = nn.Sigmoid()
        self.P, p_c = self.init_pv(pv_path)
        self.linear = nn.Linear(p_c, enc_head_kwargs['hdim'])
        T_enc = eval(enc_head_type)
        assert issubclass(T_enc, AbsEncoder)
        self._enc_head = T_enc(**enc_head_kwargs)

    def init_pv(self, fin):
        pv = torch.Tensor(np.load(fin))  
        P = nn.Parameter(pv)
        p_c = P.size()[0]
        return P, p_c
    def forward(self, x: torch.Tensor, xlens: torch.Tensor,hidden=None):
        enc_out,ls = self.T_enc(x,xlens,hidden)
        out = self.A1(self.P) # [pv_dim, 512]
        out = self.sig(out)
        out = self.A2(out) # [pv_dim, hdim]
        out = torch.einsum("bth, ch -> btc", enc_out, out)
        out = self.linear(out) # [batch, time, num_class]
        return out, ls

