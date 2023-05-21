<div align="left"><img src="doc/image/muskit_logo.png" width="550"/></div>

# Muskit: Open-source music processing toolkits

Muskit is an open-source music processing toolkit. Currently we mostly focus on benchmarking the end-to-end singing voice synthesis and expect to extend more tasks in the future. Muskit employs [pytorch](http://pytorch.org/) as a deep learning engine and also follows [ESPnet](https://github.com/espnet/espnet) and [Kaldi](http://kaldi-asr.org/) style data processing, and recipes to provide a complete setup for various music processing experiments. The main structure and base codes are adapted from ESPnet (we expect to merge the Muskit into ESPnet in later stages)

## News
The project has **been merged** to ESPnet! If you have any comments and suggestions, please feel free to discuss in **espnet**. See https://github.com/espnet/espnet/issues/4437 for details. This repo will not be maintained anymore.

## Key Features

### ESPnet style complete recipe
- Support numbers of `SVS` recipes in several databases (e.g., Kiritan, Oniku_db, Ofuton_db, Natsume database, CSD database)
- On the fly feature extraction and text processing

### SVS: Singing Voice Synthesis
- **Reproducible results** in serveral SVS public domain copora
- **Various network architecutres** for end-to-end SVS
  - RNN-based non-autoregressive model
  - Xiaoice
  - Sequence-to-sequence Transformer (with GLU-based encoder)
  - MLP singer
  - Tacotron-singing (in progress)
  - DiffSinger (to be published)
- Multi-speaker & Multilingual extention
  - Speaker ID embedding
  - Language ID embedding
  - Global sytle token (GST) embedding
- Various language support
  - Jp / En / Kr / Zh
- Integration with neural vocoders
  - the style matches the [PWG repo](https://github.com/kan-bayashi/ParallelWaveGAN) with supports of various of vocoders


### Installation
The full installation guide is available at https://github.com/SJTMusicTeam/Muskits/wiki/Installation-Instructions

### Demonstration
- Real-time SVS demo with Muskits  [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/SJTMusicTeam/svs_demo/blob/master/muskit_svs_realtime.ipynb)

### Pretrain models

Acoustic models are available at https://github.com/SJTMusicTeam/Muskits/blob/main/doc/pretrained_models.md
Vocoders are available at https://github.com/kan-bayashi/ParallelWaveGAN/blob/master/README.md

### Running instructions
The tutorial of how to use Muskits is at https://github.com/SJTMusicTeam/Muskits/blob/main/doc/tutorial.md

### Recipe Explanation
A detailed recipe explanation in https://github.com/SJTMusicTeam/Muskits/blob/main/egs/TEMPLATE/svs1/README.md
