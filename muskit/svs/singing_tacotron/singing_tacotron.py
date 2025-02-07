# Copyright 2020 Nagoya University (Tomoki Hayashi)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Tacotron 2 related modules for ESPnet2."""

import logging
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Tuple

import torch
import torch.nn.functional as F
from typeguard import check_argument_types

from muskit.torch_utils.nets_utils import make_pad_mask, make_non_pad_mask
from muskit.layers.rnn.attentions import AttForwardTA
from muskit.layers.rnn.attentions import AttForward
from muskit.layers.rnn.attentions import AttLoc
from muskit.layers.rnn.attentions import GDCAttLoc
from muskit.svs.singing_tacotron.encoder import Duration_Encoder
from muskit.svs.singing_tacotron.encoder import Content_Encoder
from muskit.svs.singing_tacotron.decoder import Decoder
from muskit.torch_utils.device_funcs import force_gatherable
from muskit.svs.abs_svs import AbsSVS
from muskit.svs.gst.style_encoder import StyleEncoder


from muskit.torch_utils.initialize import initialize
from muskit.layers.transformer.attention import MultiHeadedAttention


class GuidedAttentionLoss(torch.nn.Module):
    """Guided attention loss function module.

    This module calculates the guided attention loss described
    in `Efficiently Trainable Text-to-Speech System Based
    on Deep Convolutional Networks with Guided Attention`_,
    which forces the attention to be diagonal.

    .. _`Efficiently Trainable Text-to-Speech System
        Based on Deep Convolutional Networks with Guided Attention`:
        https://arxiv.org/abs/1710.08969

    """

    def __init__(self, sigma=0.4, alpha=1.0, reset_always=True):
        """Initialize guided attention loss module.

        Args:
            sigma (float, optional): Standard deviation to control
                how close attention to a diagonal.
            alpha (float, optional): Scaling coefficient (lambda).
            reset_always (bool, optional): Whether to always reset masks.

        """
        super(GuidedAttentionLoss, self).__init__()
        self.sigma = sigma
        self.alpha = alpha
        self.reset_always = reset_always
        self.guided_attn_masks = None
        self.masks = None

    def _reset_masks(self):
        self.guided_attn_masks = None
        self.masks = None

    def forward(self, att_ws, ilens, olens):
        """Calculate forward propagation.

        Args:
            att_ws (Tensor): Batch of attention weights (B, T_max_out, T_max_in).
            ilens (LongTensor): Batch of input lenghts (B,).
            olens (LongTensor): Batch of output lenghts (B,).

        Returns:
            Tensor: Guided attention loss value.

        """
        if self.guided_attn_masks is None:
            self.guided_attn_masks = self._make_guided_attention_masks(ilens, olens).to(
                att_ws.device
            )
        if self.masks is None:
            self.masks = self._make_masks(ilens, olens).to(att_ws.device)
        losses = self.guided_attn_masks * att_ws
        loss = torch.mean(losses.masked_select(self.masks))
        if self.reset_always:
            self._reset_masks()
        return self.alpha * loss

    def _make_guided_attention_masks(self, ilens, olens):
        n_batches = len(ilens)
        max_ilen = max(ilens)
        max_olen = max(olens)
        guided_attn_masks = torch.zeros((n_batches, max_olen, max_ilen))
        for idx, (ilen, olen) in enumerate(zip(ilens, olens)):
            guided_attn_masks[idx, :olen, :ilen] = self._make_guided_attention_mask(
                ilen, olen, self.sigma
            )
        return guided_attn_masks

    @staticmethod
    def _make_guided_attention_mask(ilen, olen, sigma):
        """Make guided attention mask.

        Examples:
            >>> guided_attn_mask =_make_guided_attention(5, 5, 0.4)
            >>> guided_attn_mask.shape
            torch.Size([5, 5])
            >>> guided_attn_mask
            tensor([[0.0000, 0.1175, 0.3935, 0.6753, 0.8647],
                    [0.1175, 0.0000, 0.1175, 0.3935, 0.6753],
                    [0.3935, 0.1175, 0.0000, 0.1175, 0.3935],
                    [0.6753, 0.3935, 0.1175, 0.0000, 0.1175],
                    [0.8647, 0.6753, 0.3935, 0.1175, 0.0000]])
            >>> guided_attn_mask =_make_guided_attention(3, 6, 0.4)
            >>> guided_attn_mask.shape
            torch.Size([6, 3])
            >>> guided_attn_mask
            tensor([[0.0000, 0.2934, 0.7506],
                    [0.0831, 0.0831, 0.5422],
                    [0.2934, 0.0000, 0.2934],
                    [0.5422, 0.0831, 0.0831],
                    [0.7506, 0.2934, 0.0000],
                    [0.8858, 0.5422, 0.0831]])

        """
        grid_x, grid_y = torch.meshgrid(torch.arange(olen), torch.arange(ilen))
        grid_x, grid_y = grid_x.float().to(olen.device), grid_y.float().to(ilen.device)
        return 1.0 - torch.exp(
            -((grid_y / ilen - grid_x / olen) ** 2) / (2 * (sigma ** 2))
        )

    @staticmethod
    def _make_masks(ilens, olens):
        """Make masks indicating non-padded part.

        Args:
            ilens (LongTensor or List): Batch of lengths (B,).
            olens (LongTensor or List): Batch of lengths (B,).

        Returns:
            Tensor: Mask tensor indicating non-padded part.
                    dtype=torch.uint8 in PyTorch 1.2-
                    dtype=torch.bool in PyTorch 1.2+ (including 1.2)

        Examples:
            >>> ilens, olens = [5, 2], [8, 5]
            >>> _make_mask(ilens, olens)
            tensor([[[1, 1, 1, 1, 1],
                     [1, 1, 1, 1, 1],
                     [1, 1, 1, 1, 1],
                     [1, 1, 1, 1, 1],
                     [1, 1, 1, 1, 1],
                     [1, 1, 1, 1, 1],
                     [1, 1, 1, 1, 1],
                     [1, 1, 1, 1, 1]],
                    [[1, 1, 0, 0, 0],
                     [1, 1, 0, 0, 0],
                     [1, 1, 0, 0, 0],
                     [1, 1, 0, 0, 0],
                     [1, 1, 0, 0, 0],
                     [0, 0, 0, 0, 0],
                     [0, 0, 0, 0, 0],
                     [0, 0, 0, 0, 0]]], dtype=torch.uint8)

        """
        in_masks = make_non_pad_mask(ilens)  # (B, T_in)
        out_masks = make_non_pad_mask(olens)  # (B, T_out)
        return out_masks.unsqueeze(-1) & in_masks.unsqueeze(-2)  # (B, T_out, T_in)


class Tacotron2Loss(torch.nn.Module):
    """Loss function module for Tacotron2."""

    def __init__(
        self, use_masking=True, use_weighted_masking=False, bce_pos_weight=20.0
    ):
        """Initialize Tactoron2 loss module.

        Args:
            use_masking (bool): Whether to apply masking
                for padded part in loss calculation.
            use_weighted_masking (bool):
                Whether to apply weighted masking in loss calculation.
            bce_pos_weight (float): Weight of positive sample of stop token.

        """
        super(Tacotron2Loss, self).__init__()
        assert (use_masking != use_weighted_masking) or not use_masking
        self.use_masking = use_masking
        self.use_weighted_masking = use_weighted_masking

        # define criterions
        reduction = "none" if self.use_weighted_masking else "mean"
        self.l1_criterion = torch.nn.L1Loss(reduction=reduction)
        self.mse_criterion = torch.nn.MSELoss(reduction=reduction)
        self.bce_criterion = torch.nn.BCEWithLogitsLoss(
            reduction=reduction, pos_weight=torch.tensor(bce_pos_weight)
        )

        # NOTE(kan-bayashi): register pre hook function for the compatibility
        self._register_load_state_dict_pre_hook(self._load_state_dict_pre_hook)

    def forward(self, after_outs, before_outs, logits, ys, labels, olens):
        """Calculate forward propagation.

        Args:
            after_outs (Tensor): Batch of outputs after postnets (B, Lmax, odim).
            before_outs (Tensor): Batch of outputs before postnets (B, Lmax, odim).
            logits (Tensor): Batch of stop logits (B, Lmax).
            ys (Tensor): Batch of padded target features (B, Lmax, odim).
            labels (LongTensor): Batch of the sequences of stop token labels (B, Lmax).
            olens (LongTensor): Batch of the lengths of each target (B,).

        Returns:
            Tensor: L1 loss value.
            Tensor: Mean square error loss value.
            Tensor: Binary cross entropy loss value.

        """
        # make mask and apply it
        if self.use_masking:
            masks = make_non_pad_mask(olens).unsqueeze(-1).to(ys.device)
            ys = ys.masked_select(masks)
            after_outs = after_outs.masked_select(masks)
            before_outs = before_outs.masked_select(masks)
            labels = labels.masked_select(masks[:, :, 0])
            logits = logits.masked_select(masks[:, :, 0])

        # calculate loss
        l1_loss = self.l1_criterion(after_outs, ys) + self.l1_criterion(before_outs, ys)
        mse_loss = self.mse_criterion(after_outs, ys) + self.mse_criterion(
            before_outs, ys
        )
        bce_loss = self.bce_criterion(logits, labels)

        # make weighted mask and apply it
        if self.use_weighted_masking:
            masks = make_non_pad_mask(olens).unsqueeze(-1).to(ys.device)
            weights = masks.float() / masks.sum(dim=1, keepdim=True).float()
            out_weights = weights.div(ys.size(0) * ys.size(2))
            logit_weights = weights.div(ys.size(0))

            # apply weight
            l1_loss = l1_loss.mul(out_weights).masked_select(masks).sum()
            mse_loss = mse_loss.mul(out_weights).masked_select(masks).sum()
            bce_loss = (
                bce_loss.mul(logit_weights.squeeze(-1))
                .masked_select(masks.squeeze(-1))
                .sum()
            )

        return l1_loss, mse_loss, bce_loss

    def _load_state_dict_pre_hook(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_msgs,
    ):
        """Apply pre hook fucntion before loading state dict.

        From v.0.6.1 `bce_criterion.pos_weight` param is registered as a parameter but
        old models do not include it and as a result, it causes missing key error when
        loading old model parameter. This function solve the issue by adding param in
        state dict before loading as a pre hook function
        of the `load_state_dict` method.

        """
        key = prefix + "bce_criterion.pos_weight"
        if key not in state_dict:
            state_dict[key] = self.bce_criterion.pos_weight


class singing_tacotron(AbsSVS):
    """singing_tacotron module for end-to-end text-to-speech.

    This is a module of Spectrogram prediction network in singing_tacotron described
    in `SINGING-TACOTRON: GLOBAL DURATION CONTROL ATTENTION AND DYNAMIC FILTER FOR END-TO-END SINGING VOICE SYNTHESIS`_,
    which converts the sequence of characters into the sequence of Mel-filterbanks.

    .. _`SINGING-TACOTRON: GLOBAL DURATION CONTROL ATTENTION AND DYNAMIC FILTER FOR END-TO-END SINGING VOICE SYNTHESIS`:
       https://arxiv.org/abs/2202.07907

    Args:
        idim (int): Dimension of the inputs.
        odim: (int) Dimension of the outputs.
        spk_embed_dim (int, optional): Dimension of the speaker embedding.
        embed_dim (int, optional): Dimension of character embedding.
        elayers (int, optional): The number of encoder blstm layers.
        eunits (int, optional): The number of encoder blstm units.
        econv_layers (int, optional): The number of encoder conv layers.
        econv_filts (int, optional): The number of encoder conv filter size.
        econv_chans (int, optional): The number of encoder conv filter channels.
        dlayers (int, optional): The number of decoder lstm layers.
        dunits (int, optional): The number of decoder lstm units.
        prenet_layers (int, optional): The number of prenet layers.
        prenet_units (int, optional): The number of prenet units.
        postnet_layers (int, optional): The number of postnet layers.
        postnet_filts (int, optional): The number of postnet filter size.
        postnet_chans (int, optional): The number of postnet filter channels.
        output_activation (str, optional): The name of activation function for outputs.
        adim (int, optional): The number of dimension of mlp in attention.
        aconv_chans (int, optional): The number of attention conv filter channels.
        aconv_filts (int, optional): The number of attention conv filter size.
        cumulate_att_w (bool, optional): Whether to cumulate previous attention weight.
        use_batch_norm (bool, optional): Whether to use batch normalization.
        use_concate (bool, optional): Whether to concatenate encoder embedding with
            decoder lstm outputs.
        reduction_factor (int, optional): Reduction factor.
        spk_embed_dim (int, optional): Number of speaker embedding dimenstions.
        spk_embed_integration_type (str, optional): How to integrate speaker embedding.
        use_gst (str, optional): Whether to use global style token.
        gst_tokens (int, optional): The number of GST embeddings.
        gst_heads (int, optional): The number of heads in GST multihead attention.
        gst_conv_layers (int, optional): The number of conv layers in GST.
        gst_conv_chans_list: (Sequence[int], optional):
            List of the number of channels of conv layers in GST.
        gst_conv_kernel_size (int, optional): Kernal size of conv layers in GST.
        gst_conv_stride (int, optional): Stride size of conv layers in GST.
        gst_gru_layers (int, optional): The number of GRU layers in GST.
        gst_gru_units (int, optional): The number of GRU units in GST.
        dropout_rate (float, optional): Dropout rate.
        zoneout_rate (float, optional): Zoneout rate.
        use_masking (bool, optional): Whether to mask padded part in loss calculation.
        use_weighted_masking (bool, optional): Whether to apply weighted masking in
            loss calculation.
        bce_pos_weight (float, optional): Weight of positive sample of stop token
            (only for use_masking=True).
        loss_type (str, optional): How to calculate loss.
        use_guided_attn_loss (bool, optional): Whether to use guided attention loss.
        guided_attn_loss_sigma (float, optional): Sigma in guided attention loss.
        guided_attn_loss_lamdba (float, optional): Lambda in guided attention loss.

    """

    def __init__(
        self,
        # network structure related
        idim: int,
        midi_dim: int,
        tempo_dim: int,
        odim: int,
        embed_dim: int = 512,
        elayers: int = 1,
        eunits: int = 512,
        econv_layers: int = 3,
        econv_chans: int = 512,
        econv_filts: int = 5,
        atype: str = "GDCA_location",
        adim: int = 512,
        aconv_chans: int = 32,
        aconv_filts: int = 15,
        cumulate_att_w: bool = True,
        dlayers: int = 2,
        dunits: int = 1024,
        prenet_layers: int = 2,
        prenet_units: int = 256,
        postnet_layers: int = 5,
        postnet_chans: int = 512,
        postnet_filts: int = 5,
        output_activation: str = None,
        use_batch_norm: bool = True,
        use_concate: bool = True,
        use_residual: bool = False,
        reduction_factor: int = 1,
        use_gst: bool = False,
        gst_tokens: int = 10,
        gst_heads: int = 4,
        gst_conv_layers: int = 6,
        gst_conv_chans_list: Sequence[int] = (32, 32, 64, 64, 128, 128),
        gst_conv_kernel_size: int = 3,
        gst_conv_stride: int = 2,
        gst_gru_layers: int = 1,
        gst_gru_units: int = 128,
        # training related
        dropout_rate: float = 0.5,
        zoneout_rate: float = 0.1,
        use_masking: bool = True,
        use_weighted_masking: bool = False,
        bce_pos_weight: float = 5.0,
        loss_type: str = "L1",
        use_guided_attn_loss: bool = True,
        guided_attn_loss_sigma: float = 0.4,
        guided_attn_loss_lambda: float = 1.0,
        # extra embedding related
        spks: Optional[int] = None,
        langs: Optional[int] = None,
        spk_embed_dim: Optional[int] = None,
        spk_embed_integration_type: str = "concat",
    ):
        """Initialize singing_tacotron module."""
        assert check_argument_types()
        super().__init__()

        # store hyperparameters
        self.idim = idim
        self.odim = odim
        self.midi_dim = midi_dim
        self.tempo_dim = tempo_dim
        self.embed_dim = embed_dim
        self.eos = idim - 1
        self.spk_embed_dim = spk_embed_dim
        self.cumulate_att_w = cumulate_att_w
        self.reduction_factor = reduction_factor
        self.use_gst = use_gst
        self.use_guided_attn_loss = use_guided_attn_loss
        self.loss_type = loss_type
        self.atype = atype
        if self.spk_embed_dim is not None:
            self.spk_embed_integration_type = spk_embed_integration_type

        # define activation function for the final output
        if output_activation is None:
            self.output_activation_fn = None
        elif hasattr(F, output_activation):
            self.output_activation_fn = getattr(F, output_activation)
        else:
            raise ValueError(
                f"there is no such an activation function. " f"({output_activation})"
            )

        # set padding idx
        self.padding_idx = 0

        # define encoder
        self.phone_encode_layer = torch.nn.Embedding(
            num_embeddings=idim, embedding_dim=embed_dim, padding_idx=self.padding_idx
        )
        self.midi_encode_layer = torch.nn.Embedding(
            num_embeddings=midi_dim,
            embedding_dim=embed_dim,
            padding_idx=self.padding_idx,
        )
        self.tempo_encode_layer = torch.nn.Embedding(
            num_embeddings=tempo_dim,
            embedding_dim=embed_dim,
            padding_idx=self.padding_idx,
        )
        self.duration_encode_layer = torch.nn.Linear(1, embed_dim)

        if self.atype == "GDCA_location":
            coeff = 2
        else:
            coeff = 4
        
        self.enc_content = Content_Encoder(
            idim=coeff*embed_dim,
            embed_dim=embed_dim,
            elayers=elayers,
            eunits=eunits,
            econv_layers=econv_layers,
            econv_chans=econv_chans,
            econv_filts=econv_filts,
            use_batch_norm=use_batch_norm,
            use_residual=use_residual,
            dropout_rate=dropout_rate,
            padding_idx=self.padding_idx,
        )

        self.enc_duration = Duration_Encoder(
            idim=2*embed_dim,
            embed_dim=embed_dim,
            dropout_rate=dropout_rate,
            padding_idx=self.padding_idx,
        )

        # define network modules
        if self.use_gst:
            self.gst = StyleEncoder(
                idim=odim,  # the input is mel-spectrogram
                gst_tokens=gst_tokens,
                gst_token_dim=eunits,
                gst_heads=gst_heads,
                conv_layers=gst_conv_layers,
                conv_chans_list=gst_conv_chans_list,
                conv_kernel_size=gst_conv_kernel_size,
                conv_stride=gst_conv_stride,
                gru_layers=gst_gru_layers,
                gru_units=gst_gru_units,
            )

        # define spk and lang embedding
        self.spks = None
        if spks is not None and spks > 1:
            self.spks = spks
            self.sid_emb = torch.nn.Embedding(spks, adim)
        self.langs = None
        if langs is not None and langs > 1:
            self.langs = langs
            self.lid_emb = torch.nn.Embedding(langs, adim)

        # define additional projection for speaker embedding
        self.spk_embed_dim = None
        if spk_embed_dim is not None and spk_embed_dim > 0:
            self.spk_embed_dim = spk_embed_dim
            self.spk_embed_integration_type = spk_embed_integration_type
        if self.spk_embed_dim is not None:
            if self.spk_embed_integration_type == "add":
                dec_idim = eunits
                self.projection = torch.nn.Linear(self.spk_embed_dim, adim)
            elif spk_embed_integration_type == "concat":
                dec_idim = eunits + spk_embed_dim
                self.projection = torch.nn.Linear(adim + self.spk_embed_dim, adim)
            else:
                raise ValueError(f"{spk_embed_integration_type} is not supported.")
        else:
            dec_idim = eunits

        if atype == "location":
            att = AttLoc(dec_idim, dunits, adim, aconv_chans, aconv_filts)
        elif atype == "GDCA_location":
            att = GDCAttLoc(dec_idim, dunits, adim, aconv_chans, aconv_filts)
        elif atype == "forward":
            att = AttForward(dec_idim, dunits, adim, aconv_chans, aconv_filts)
            if self.cumulate_att_w:
                logging.warning(
                    "cumulation of attention weights is disabled "
                    "in forward attention."
                )
                self.cumulate_att_w = False
        elif atype == "forward_ta":
            att = AttForwardTA(dec_idim, dunits, adim, aconv_chans, aconv_filts, odim)
            if self.cumulate_att_w:
                logging.warning(
                    "cumulation of attention weights is disabled "
                    "in forward attention."
                )
                self.cumulate_att_w = False
        else:
            raise NotImplementedError("Support only location or forward")

        # define duration predictor
#         self.duration_predictor = DurationPredictor(
#             idim=adim,
#             n_layers=duration_predictor_layers,
#             n_chans=duration_predictor_chans,
#             kernel_size=duration_predictor_kernel_size,
#             dropout_rate=duration_predictor_dropout_rate,
#         )

        # define decoder
        self.dec = Decoder(
            idim=dec_idim,
            odim=odim,
            att=att,
            dlayers=dlayers,
            dunits=dunits,
            prenet_layers=prenet_layers,
            prenet_units=prenet_units,
            postnet_layers=postnet_layers,
            postnet_chans=postnet_chans,
            postnet_filts=postnet_filts,
            output_activation_fn=self.output_activation_fn,
            cumulate_att_w=self.cumulate_att_w,
            use_batch_norm=use_batch_norm,
            use_concate=use_concate,
            dropout_rate=dropout_rate,
            zoneout_rate=zoneout_rate,
            reduction_factor=reduction_factor,
        )
        self.taco2_loss = Tacotron2Loss(
            use_masking=use_masking,
            use_weighted_masking=use_weighted_masking,
            bce_pos_weight=bce_pos_weight,
        )
        if self.use_guided_attn_loss:
            self.attn_loss = GuidedAttentionLoss(
                sigma=guided_attn_loss_sigma,
                alpha=guided_attn_loss_lambda,
            )

        # initialize parameters
#         self._reset_parameters(
#             init_type=init_type,
#             init_enc_alpha=init_enc_alpha,
#             init_dec_alpha=init_dec_alpha,
#         )

    def forward(
        self,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        feats: torch.Tensor,
        feats_lengths: torch.Tensor,
        label: torch.Tensor,
        label_lengths: torch.Tensor,
        midi: torch.Tensor,
        midi_lengths: torch.Tensor,
        tempo: torch.Tensor,
        tempo_lengths: torch.Tensor,
        ds: torch.Tensor,
        spembs: Optional[torch.Tensor] = None,
        sids: Optional[torch.Tensor] = None,
        lids: Optional[torch.Tensor] = None,
        flag_IsValid=False,
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor], torch.Tensor]:
        """Calculate forward propagation.

        Args:
            text (LongTensor): Batch of padded character ids (B, Tmax).
            text_lengths (LongTensor): Batch of lengths of each input batch (B,).
            feats (Tensor): Batch of padded target features (B, Lmax, odim).
            feats_lengths (LongTensor): Batch of the lengths of each target (B,).
            spembs (Tensor, optional): Batch of speaker embeddings (B, spk_embed_dim).
            ds: durations (LongTensor) Batch of padded durations (B, T_text + 1).
            // durations_lengths (LongTensor): Batch of duration lengths (B, T_text + 1).
            spembs (Optional[Tensor]): Batch of speaker embeddings (B, spk_embed_dim).
            sids (Optional[Tensor]): Batch of speaker IDs (B, 1).
            lids (Optional[Tensor]): Batch of language IDs (B, 1).

        Returns:
            Tensor: Loss scalar value.
            Dict: Statistics to be monitored.
            Tensor: Weight value.

        """
        text = text[:, : text_lengths.max()]  # for data-parallel
        feats = feats[:, : feats_lengths.max()]  # for data-parallel
        midi = midi[:, : midi_lengths.max()]  # for data-parallel
        label = label[:, : label_lengths.max()]  # for data-parallel
        tempo = tempo[:, : tempo_lengths.max()]  # for data-parallel

        batch_size = text.size(0)

        label_emb = self.phone_encode_layer(label)
        midi_emb = self.midi_encode_layer(midi)
        tempo_emb = self.tempo_encode_layer(tempo) # FIX ME (Nan): the tempo of singing tacotron is BPM, should change later.
        ds_tensor = torch.tensor(ds.unsqueeze(-1),dtype=torch.float32).cuda()
        ds_emb = self.duration_encode_layer(ds_tensor)
        
        content_input = torch.cat([label_emb, midi_emb], dim=-1) # cat this two or cat 4 into content_enc
        duration_tempo = torch.cat([tempo_emb, ds_emb], dim=-1)
        
        att_input = None
        if self.atype != "GDCA_location":
            att_input = torch.cat([content_input, duration_tempo], dim=-1)

        # TODO (Nan): add start & End token
        # # Add eos at the last of sequence
        # xs = F.pad(text, [0, 1], "constant", self.padding_idx)
        # for i, l in enumerate(text_lengths):
        #     xs[i, l] = self.eos
        # ilens = text_lengths + 1

        ys = feats
        olens = feats_lengths
        ilens = label_lengths

        # make labels for stop prediction  
        # TODO: (Nan) change name
        stop_labels = make_pad_mask(olens - 1).to(ys.device, ys.dtype)
        stop_labels = F.pad(stop_labels, [0, 1], "constant", 1.0)

        # calculate tacotron2 outputs
        after_outs, before_outs, logits, att_ws = self._forward(
            content_input, att_input, duration_tempo, ilens, ys, olens, spembs
        )

        # modify mod part of groundtruth
        if self.reduction_factor > 1:
            olens = olens.new([olen - olen % self.reduction_factor for olen in olens])
            max_out = max(olens)
            ys = ys[:, :max_out]
            stop_labels = stop_labels[:, :max_out]
            stop_labels[:, -1] = 1.0  # make sure at least one frame has 1
        else:
            ys = feats
            olens = feats_lengths

        # calculate taco2 loss
        l1_loss, mse_loss, bce_loss = self.taco2_loss(
            after_outs, before_outs, logits, ys, stop_labels, olens
        )
        if self.loss_type == "L1+L2":
            loss = l1_loss + mse_loss + bce_loss
        elif self.loss_type == "L1":
            loss = l1_loss + bce_loss
        elif self.loss_type == "L2":
            loss = mse_loss + bce_loss
        else:
            raise ValueError(f"unknown --loss-type {self.loss_type}")

        stats = dict(
            l1_loss=l1_loss.item(),
            mse_loss=mse_loss.item(),
            bce_loss=bce_loss.item(),
        )

        # calculate attention loss
        if self.use_guided_attn_loss:
            # NOTE(kan-bayashi): length of output for auto-regressive
            # input will be changed when r > 1
            if self.reduction_factor > 1:
                olens_in = olens.new([olen // self.reduction_factor for olen in olens])
            else:
                olens_in = olens
            attn_loss = self.attn_loss(att_ws, ilens, olens_in)
            loss = loss + attn_loss
            stats.update(attn_loss=attn_loss.item())

        stats.update(loss=loss.item())

        loss, stats, weight = force_gatherable((loss, stats, batch_size), loss.device)
        
        if flag_IsValid == False:
            # train stage
            return loss, stats, weight
        else:
            # validation stage
            return loss, stats, weight, after_outs[:, : olens.max()], ys, olens

    def _forward(
        self,
        xs: torch.Tensor,
        att_input: torch.Tensor,
        duration_tempo: torch.Tensor,
        ilens: torch.Tensor,
        ys: torch.Tensor,
        olens: torch.Tensor,
        spembs: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        if self.atype == "GDCA_location":
            hs, hlens = self.enc_content(xs, ilens) # hs: (B, seq_len, emb_dim)
            trans_token = self.enc_duration(duration_tempo) # (B, seq_len, 1)
        else:
            hs, hlens = self.enc_content(att_input, ilens) # hs: (B, seq_len, emb_dim)
            trans_token = None
        if self.use_gst:
            style_embs = self.gst(ys)
            hs = hs + style_embs.unsqueeze(1)
        if self.spk_embed_dim is not None:
            hs = self._integrate_with_spk_embed(hs, spembs)
        
        return self.dec(hs, hlens, trans_token, ys)

    def inference(
        self,
        text: torch.Tensor,
        label: torch.Tensor,
        midi: torch.Tensor,
        ds: torch.Tensor,
        feats: torch.Tensor = None,
        threshold: float = 0.5,
        minlenratio: float = 0.0,
        maxlenratio: float = 10.0,
        use_att_constraint: bool = False,
        backward_window: int = 1,
        forward_window: int = 3,
        use_teacher_forcing: bool = False,
        tempo: Optional[torch.Tensor] = None,
        spembs: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Generate the sequence of features given the sequences of characters.

        Args:
            text (LongTensor): Input sequence of characters (T,).
            feats (Tensor, optional): Feature sequence to extract style (N, idim).
            ds: durations (Optional[LongTensor]) Groundtruth of duration (T_text + 1,).
            spembs (Optional[Tensor]): Speaker embedding (spk_embed_dim,).
            sids (Optional[Tensor]): Speaker ID (1,).
            lids (Optional[Tensor]): Language ID (1,).
            alpha (float): Alpha to control the speed.
            threshold (float, optional): Threshold in inference.
            minlenratio (float, optional): Minimum length ratio in inference.
            maxlenratio (float, optional): Maximum length ratio in inference.
            use_att_constraint (bool, optional): Whether to apply attention constraint.
            backward_window (int, optional): Backward window in attention constraint.
            forward_window (int, optional): Forward window in attention constraint.
            use_teacher_forcing (bool, optional): Whether to use teacher forcing.

        Returns:
            Tensor: Output sequence of features (L, odim).
            Tensor: Output sequence of stop probabilities (L,).
            Tensor: Attention weights (L, T).

        """
        label_emb = self.phone_encode_layer(label)
        midi_emb = self.midi_encode_layer(midi)
        tempo_emb = self.tempo_encode_layer(tempo) # FIX ME (Nan): the tempo of singing tacotron is BPM, should change later.
        ds_tensor = torch.as_tensor(ds.unsqueeze(-1),dtype=torch.float32).to(midi.device)
#         ds_tensor = ds.unsqueeze(-1).to(midi.device).to(dtype=torch.float32)
        ds_emb = self.duration_encode_layer(ds_tensor)
        
        content_input = torch.cat([label_emb, midi_emb], dim=-1) # cat this two or cat 4 into content_enc
        duration_tempo = torch.cat([tempo_emb, ds_emb], dim=-1)

        att_input = None
        if self.atype != "GDCA_location":
            att_input = torch.cat([content_input, duration_tempo], dim=-1)

        x = text
        y = feats
        spemb = spembs

        # TODO: (Nan)
        # # add eos at the last of sequence
        # x = F.pad(x, [0, 1], "constant", self.eos)

        # inference with teacher forcing
        if use_teacher_forcing:
            assert feats is not None, "feats must be provided with teacher forcing."

            xs, ys = x.unsqueeze(0), y.unsqueeze(0)
            spembs = None if spemb is None else spemb.unsqueeze(0)
            ilens = x.new_tensor([xs.size(1)]).long()
            olens = y.new_tensor([ys.size(1)]).long()
            outs, _, _, att_ws = self._forward(xs, ilens, ys, olens, spembs)

            return outs[0], None, att_ws[0]

        # inference
        if self.atype == "GDCA_location":
            h = self.enc_content.inference(content_input) # h: (B, seq_len, emb_dim)
            trans_token = self.enc_duration.inference(duration_tempo) # (B, seq_len, 1)
        else:
            h = self.enc_content.inference(att_input) # hs: (B, seq_len, emb_dim)
            trans_token = None
            
        # integrate with SID and LID embeddings
        if self.spks is not None:
            sid_embs = self.sid_emb(sids.view(-1))
            h = h + sid_embs.unsqueeze(1)
        if self.langs is not None:
            lid_embs = self.lid_emb(lids.view(-1))
            h = h + lid_embs.unsqueeze(1)
            
        # integrate speaker embedding
        if self.spk_embed_dim is not None:
            h = self._integrate_with_spk_embed(h, spembs)
            
        if self.use_gst:
            style_emb = self.gst(y.unsqueeze(0))
            h = h + style_emb
        if self.spk_embed_dim is not None:
            hs, spembs = h.unsqueeze(0), spemb.unsqueeze(0)
            h = self._integrate_with_spk_embed(hs, spembs)[0]
        outs, probs, att_ws = self.dec.inference(
            h,
            trans_token,
            threshold=threshold,
            minlenratio=minlenratio,
            maxlenratio=maxlenratio,
            use_att_constraint=use_att_constraint,
            backward_window=backward_window,
            forward_window=forward_window,
        )

        return outs, probs, att_ws

    def _integrate_with_spk_embed(
        self, hs: torch.Tensor, spembs: torch.Tensor
    ) -> torch.Tensor:
        """Integrate speaker embedding with hidden states.

        Args:
            hs (Tensor): Batch of hidden state sequences (B, Tmax, eunits).
            spembs (Tensor): Batch of speaker embeddings (B, spk_embed_dim).

        Returns:
            Tensor: Batch of integrated hidden state sequences (B, Tmax, eunits) if
                integration_type is "add" else (B, Tmax, eunits + spk_embed_dim).

        """
        if self.spk_embed_integration_type == "add":
            # apply projection and then add to hidden states
            spembs = self.projection(F.normalize(spembs))
            hs = hs + spembs.unsqueeze(1)
        elif self.spk_embed_integration_type == "concat":
            # concat hidden states with spk embeds
            spembs = F.normalize(spembs).unsqueeze(1).expand(-1, hs.size(1), -1)
            hs = torch.cat([hs, spembs], dim=-1)
        else:
            raise NotImplementedError("support only add or concat.")

        return hs