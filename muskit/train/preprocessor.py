from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Collection
from typing import Dict
from typing import Iterable
from typing import Union

import logging
import numpy as np
import scipy.signal
import soundfile
import random
import pytsmod as tsm
from typeguard import check_argument_types
from typeguard import check_return_type

from muskit.text.build_tokenizer import build_tokenizer
from muskit.text.cleaner import TextCleaner
from muskit.text.token_id_converter import TokenIDConverter


class AbsPreprocessor(ABC):
    def __init__(self, train: bool):
        self.train = train

    @abstractmethod
    def __call__(
        self, 
        uid: str, 
        data: Dict[str, Union[str, np.ndarray]],
        phone_time_aug_factor: float
    ) -> Dict[str, np.ndarray]:
        raise NotImplementedError


def framing(
    x,
    frame_length: int = 512,
    frame_shift: int = 256,
    centered: bool = True,
    padded: bool = True,
):
    if x.size == 0:
        raise ValueError("Input array size is zero")
    if frame_length < 1:
        raise ValueError("frame_length must be a positive integer")
    if frame_length > x.shape[-1]:
        raise ValueError("frame_length is greater than input length")
    if 0 >= frame_shift:
        raise ValueError("frame_shift must be greater than 0")

    if centered:
        pad_shape = [(0, 0) for _ in range(x.ndim - 1)] + [
            (frame_length // 2, frame_length // 2)
        ]
        x = np.pad(x, pad_shape, mode="constant", constant_values=0)

    if padded:
        # Pad to integer number of windowed segments
        # I.e make x.shape[-1] = frame_length + (nseg-1)*nstep,
        #  with integer nseg
        nadd = (-(x.shape[-1] - frame_length) % frame_shift) % frame_length
        pad_shape = [(0, 0) for _ in range(x.ndim - 1)] + [(0, nadd)]
        x = np.pad(x, pad_shape, mode="constant", constant_values=0)

    # Created strided array of data segments
    if frame_length == 1 and frame_length == frame_shift:
        result = x[..., None]
    else:
        shape = x.shape[:-1] + (
            (x.shape[-1] - frame_length) // frame_shift + 1,
            frame_length,
        )
        strides = x.strides[:-1] + (frame_shift * x.strides[-1], x.strides[-1])
        result = np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides)
    return result


def detect_non_silence(
    x: np.ndarray,
    threshold: float = 0.01,
    frame_length: int = 1024,
    frame_shift: int = 512,
    window: str = "boxcar",
) -> np.ndarray:
    """Power based voice activity detection.

    Args:
        x: (Channel, Time)

    >>> x = np.random.randn(1000)
    >>> detect = detect_non_silence(x)
    >>> assert x.shape == detect.shape
    >>> assert detect.dtype == np.bool

    """
    if x.shape[-1] < frame_length:
        return np.full(x.shape, fill_value=True, dtype=np.bool)

    if x.dtype.kind == "i":
        x = x.astype(np.float64)
    # framed_w: (C, T, F)
    framed_w = framing(
        x,
        frame_length=frame_length,
        frame_shift=frame_shift,
        centered=False,
        padded=True,
    )
    framed_w *= scipy.signal.get_window(window, frame_length).astype(framed_w.dtype)
    # power: (C, T)
    power = (framed_w ** 2).mean(axis=-1)
    # mean_power: (C,)
    mean_power = power.mean(axis=-1)
    if np.all(mean_power == 0):
        return np.full(x.shape, fill_value=True, dtype=np.bool)
    # detect_frames: (C, T)
    detect_frames = power / mean_power > threshold
    # detects: (C, T, F)
    detects = np.broadcast_to(
        detect_frames[..., None], detect_frames.shape + (frame_shift,)
    )
    # detects: (C, TF)
    detects = detects.reshape(*detect_frames.shape[:-1], -1)
    # detects: (C, TF)
    return np.pad(
        detects,
        [(0, 0)] * (x.ndim - 1) + [(0, x.shape[-1] - detects.shape[-1])],
        mode="edge",
    )


class CommonPreprocessor(AbsPreprocessor):
    def __init__(
        self,
        train: bool,
        token_type: str = None,
        token_list: Union[Path, str, Iterable[str]] = None,
        bpemodel: Union[Path, str, Iterable[str]] = None,
        text_cleaner: Collection[str] = None,
        g2p_type: str = None,
        unk_symbol: str = "<unk>",
        space_symbol: str = "<space>",
        non_linguistic_symbols: Union[Path, str, Iterable[str]] = None,
        delimiter: str = None,
        rir_scp: str = None,
        rir_apply_prob: float = 1.0,
        noise_scp: str = None,
        noise_apply_prob: float = 1.0,
        noise_db_range: str = "3_10",
        singing_volume_normalize: float = None,
        singing_name: str = "singing",
        text_name: str = "text",
        label_name: str = "label",
        midi_name: str = "midi",
        fs: np.int32 = 0,
    ):
        super().__init__(train)
        self.train = train
        self.singing_name = singing_name
        self.text_name = text_name
        self.label_name = label_name
        self.midi_name = midi_name
        self.fs = fs
        self.singing_volume_normalize = singing_volume_normalize
        self.rir_apply_prob = rir_apply_prob
        self.noise_apply_prob = noise_apply_prob

        if token_type is not None:
            if token_list is None:
                raise ValueError("token_list is required if token_type is not None")
            self.text_cleaner = TextCleaner(text_cleaner)

            self.tokenizer = build_tokenizer(
                token_type=token_type,
                bpemodel=bpemodel,
                delimiter=delimiter,
                space_symbol=space_symbol,
                non_linguistic_symbols=non_linguistic_symbols,
                g2p_type=g2p_type,
            )
            self.token_id_converter = TokenIDConverter(
                token_list=token_list, unk_symbol=unk_symbol,
            )
        else:
            self.text_cleaner = None
            self.tokenizer = None
            self.token_id_converter = None

        if train and rir_scp is not None:
            self.rirs = []
            with open(rir_scp, "r", encoding="utf-8") as f:
                for line in f:
                    sps = line.strip().split(None, 1)
                    if len(sps) == 1:
                        self.rirs.append(sps[0])
                    else:
                        self.rirs.append(sps[1])
        else:
            self.rirs = None

        if train and noise_scp is not None:
            self.noises = []
            with open(noise_scp, "r", encoding="utf-8") as f:
                for line in f:
                    sps = line.strip().split(None, 1)
                    if len(sps) == 1:
                        self.noises.append(sps[0])
                    else:
                        self.noises.append(sps[1])
            sps = noise_db_range.split("_")
            if len(sps) == 1:
                self.noise_db_low, self.noise_db_high = float(sps[0])
            elif len(sps) == 2:
                self.noise_db_low, self.noise_db_high = float(sps[0]), float(sps[1])
            else:
                raise ValueError(
                    "Format error: '{noise_db_range}' e.g. -3_4 -> [-3db,4db]"
                )
        else:
            self.noises = None

    def __call__(
        self, uid: str, data: Dict[str, Union[str, np.ndarray, tuple]], phone_time_aug_factor: float
    ) -> Dict[str, np.ndarray]:
        assert check_argument_types()
        assert phone_time_aug_factor >= 1   # support longer only

        if self.midi_name in data and self.tokenizer is not None:
            pitchseq, temposeq = data[self.midi_name]
            nsamples = len(pitchseq)
            pitchseq.astype(np.int64)
            temposeq.astype(np.int64)
            data.pop(self.midi_name)

            data["score"] = pitchseq
            data["tempo"] = temposeq

        if self.label_name in data and self.tokenizer is not None:
            timeseq, text = data[self.label_name]
            # if not isinstance(text, np.ndarray):
            text = " ".join(text)
            text = self.text_cleaner(text)
            tokens = self.tokenizer.text2tokens(text)
            text_ints = self.token_id_converter.tokens2ids(tokens)

            vowel_tokens = ["a", "e", "i", "o", "u"]
            vowel_ints = self.token_id_converter.tokens2ids(vowel_tokens)

            data.pop(self.label_name)
            # [Shuai]: length of label - phone_id seq is the same of midi, 
            # global_time_aug_factor has already been applied on midi length in dataset.py, step1. Load data from each loaders
            # so the global_time_aug_factor won`t be applied here when init.
            labelseq = np.zeros((nsamples))
            offset = timeseq[0, 0]
            anchor_pairs = []
            for i in range(timeseq.shape[0]):
                start = int((timeseq[i, 0] - offset) * self.fs)
                end = int((timeseq[i, 1] - offset) * self.fs) + 1
                if end > nsamples:
                    end = nsamples - 1
                labelseq[start:end] = text_ints[i]
                
                # phone-level augmentation for vowels
                if text_ints[i] in vowel_ints and phone_time_aug_factor != 1.0:
                    if random.random() < 0.5:
                        anchor_pairs.append( (start, end, text_ints[i]) )
            # logging.info(f"anchor_pairs: {anchor_pairs}， uid: {uid}, phone_time_aug_factor: {phone_time_aug_factor}")

            # phone-level augmentation
            if len(anchor_pairs) != 0:
                insert_indexes_label = []
                insert_values_label = []
                insert_values_score = []
                insert_values_tempo = []
                insert_num_list = []
                for anchor_pair in anchor_pairs:
                    start, end, _label = anchor_pair
                    index_gap_origin = end - start
                    index_gap_aug = (end - start) * phone_time_aug_factor
                    insert_num = int(index_gap_aug - index_gap_origin)
                    insert_num_list.append(insert_num)

                    insert_indexes_label += [end for _ in range(insert_num)]
                    insert_values_label += [_label for _ in range(insert_num)]

                    # logging.info(f"end: {end}, nsamples: {nsamples}")
                    insert_values_score += [data['score'][end] for _ in range(insert_num)]
                    insert_values_tempo += [data['tempo'][end] for _ in range(insert_num)]

                labelseq = np.insert(labelseq, insert_indexes_label, insert_values_label)

                data["score"] = np.insert(data["score"], insert_indexes_label, insert_values_score)
                data["tempo"] = np.insert(data["tempo"], insert_indexes_label, insert_values_tempo)
            labelseq.astype(np.int64)
            data["durations"] = labelseq

        if self.singing_name in data:
            # logging.info(f"In self.singing_name, len(anchor_pairs): {len(anchor_pairs)}")

            # phone-level augmentation
            insert_accumulative = 0
            if phone_time_aug_factor != 1.0:
                if len(anchor_pairs) != 0:
                    singing = data[self.singing_name]
                    nsamples = len(singing)
                    s_ap = [[0],[0]]
                    for i in range(len(anchor_pairs)):
                        start, end, _ = anchor_pairs[i]
                        if start != 0:
                            s_ap[0].append(start)
                        s_ap[0].append(end)

                        insert_num = insert_num_list[i]
                        if start + insert_accumulative != 0:
                            s_ap[1].append(start + insert_accumulative)
                        s_ap[1].append(end + insert_accumulative + insert_num)
                        insert_accumulative += insert_num
                    if end != nsamples:
                        s_ap[0].append(nsamples)
                        s_ap[1].append(nsamples + insert_accumulative)

                    # logging.info(f"s_ap: {s_ap}, ndim of s_ap: {np.array(s_ap).ndim}, phone_time_aug_factor: {phone_time_aug_factor}")
                    assert np.array(s_ap).ndim == 2
                    singing = tsm.wsola(singing, np.array(s_ap))
                    # logging.info(f"singing: {singing.shape}, nsamples: {nsamples}, phone_time_aug_factor: {phone_time_aug_factor}")
                    data[self.singing_name] = singing

            # quit()

            if self.train and self.rirs is not None and self.noises is not None:
                singing = data[self.singing_name]
                nsamples = len(singing)

                # singing: (Nmic, Time)
                if singing.ndim == 1:
                    singing = singing[None, :]
                else:
                    singing = singing.T
                # Calc power on non shlence region
                power = (singing[detect_non_silence(singing)] ** 2).mean()

                # 1. Convolve RIR
                if self.rirs is not None and self.rir_apply_prob >= np.random.random():
                    rir_path = np.random.choice(self.rirs)
                    if rir_path is not None:
                        rir, _ = soundfile.read(
                            rir_path, dtype=np.float64, always_2d=True
                        )

                        # rir: (Nmic, Time)
                        rir = rir.T

                        # singing: (Nmic, Time)
                        # Note that this operation doesn't change the signal length
                        singing = scipy.signal.convolve(singing, rir, mode="full")[
                            :, : singing.shape[1]
                        ]
                        # Reverse mean power to the original power
                        power2 = (singing[detect_non_silence(singing)] ** 2).mean()
                        singing = np.sqrt(power / max(power2, 1e-10)) * singing

                # 2. Add Noise
                if (
                    self.noises is not None
                    and self.rir_apply_prob >= np.random.random()
                ):
                    noise_path = np.random.choice(self.noises)
                    if noise_path is not None:
                        noise_db = np.random.uniform(
                            self.noise_db_low, self.noise_db_high
                        )
                        with soundfile.SoundFile(noise_path) as f:
                            if f.frames == nsamples:
                                noise = f.read(dtype=np.float64, always_2d=True)
                            elif f.frames < nsamples:
                                offset = np.random.randint(0, nsamples - f.frames)
                                # noise: (Time, Nmic)
                                noise = f.read(dtype=np.float64, always_2d=True)
                                # Repeat noise
                                noise = np.pad(
                                    noise,
                                    [(offset, nsamples - f.frames - offset), (0, 0)],
                                    mode="wrap",
                                )
                            else:
                                offset = np.random.randint(0, f.frames - nsamples)
                                f.seek(offset)
                                # noise: (Time, Nmic)
                                noise = f.read(
                                    nsamples, dtype=np.float64, always_2d=True
                                )
                                if len(noise) != nsamples:
                                    raise RuntimeError(f"Something wrong: {noise_path}")
                        # noise: (Nmic, Time)
                        noise = noise.T

                        noise_power = (noise ** 2).mean()
                        scale = (
                            10 ** (-noise_db / 20)
                            * np.sqrt(power)
                            / np.sqrt(max(noise_power, 1e-10))
                        )
                        singing = singing + scale * noise

                singing = singing.T
                ma = np.max(np.abs(singing))
                if ma > 1.0:
                    singing /= ma
                data[self.singing_name] = singing

            if self.singing_volume_normalize is not None:
                singing = data[self.singing_name]
                ma = np.max(np.abs(singing))
                data[self.singing_name] = singing * self.singing_volume_normalize / ma
        
        if self.text_name in data and self.tokenizer is not None:
            text = data[self.text_name]
            # logging.info(f"uid: {uid}, text: {text}")
            if not isinstance(text, np.ndarray):
                if not isinstance(text, str):
                    text = " ".join(text)
                text = self.text_cleaner(text)
                tokens = self.tokenizer.text2tokens(text)
                _text_ints = self.token_id_converter.tokens2ids(tokens)
                # assert text_ints == _text_ints
                data[self.text_name] = np.array(_text_ints, dtype=np.int64)
        # TODO allow the tuple type
        # assert check_return_type(data)
        # logging.info(f"uid: {uid}, data: {data}")
        return data


class CommonPreprocessor_multi(AbsPreprocessor):
    def __init__(
        self,
        train: bool,
        token_type: str = None,
        token_list: Union[Path, str, Iterable[str]] = None,
        bpemodel: Union[Path, str, Iterable[str]] = None,
        text_cleaner: Collection[str] = None,
        g2p_type: str = None,
        unk_symbol: str = "<unk>",
        space_symbol: str = "<space>",
        non_linguistic_symbols: Union[Path, str, Iterable[str]] = None,
        delimiter: str = None,
        singing_name: str = "singing",
        text_name: list = ["text"],
    ):
        super().__init__(train)
        self.train = train
        self.singing_name = singing_name
        self.text_name = text_name

        if token_type is not None:
            if token_list is None:
                raise ValueError("token_list is required if token_type is not None")
            self.text_cleaner = TextCleaner(text_cleaner)

            self.tokenizer = build_tokenizer(
                token_type=token_type,
                bpemodel=bpemodel,
                delimiter=delimiter,
                space_symbol=space_symbol,
                non_linguistic_symbols=non_linguistic_symbols,
                g2p_type=g2p_type,
            )
            self.token_id_converter = TokenIDConverter(
                token_list=token_list, unk_symbol=unk_symbol,
            )
        else:
            self.text_cleaner = None
            self.tokenizer = None
            self.token_id_converter = None

    def __call__(
        self, uid: str, data: Dict[str, Union[str, np.ndarray]]
    ) -> Dict[str, np.ndarray]:
        assert check_argument_types()

        if self.singing_name in data:
            # Nothing now: candidates:
            # - STFT
            # - Fbank
            # - CMVN
            # - Data augmentation
            pass

        for text_n in self.text_name:
            if text_n in data and self.tokenizer is not None:
                text = data[text_n]
                text = self.text_cleaner(text)
                tokens = self.tokenizer.text2tokens(text)
                text_ints = self.token_id_converter.tokens2ids(tokens)
                data[text_n] = np.array(text_ints, dtype=np.int64)
        assert check_return_type(data)
        return data
