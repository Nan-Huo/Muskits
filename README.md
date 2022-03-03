<div align="left"><img src="doc/image/muskit_logo.png" width="550"/></div>

# Muskit: Open-source music processing toolkits

Muskit is an open-source music processing toolkit. Currently we mostly focus on benchmarking the end-to-end singing voice synthesis and expect to extend more tasks in the future. Muskit employs [pytorch](http://pytorch.org/) as a deep learning engine and also follows [ESPnet](https://github.com/espnet/espnet) and [Kaldi](http://kaldi-asr.org/) style data processing, and recipes to provide a complete setup for various music processing experiments. The main structure and base codes are adapted from ESPnet (we expect to merge the Muskit into ESPnet in later stages)

## We want to note that the toolkit is still pre-released. There are several issues with the code for final usage and we prepare to refine it towards first release by the end of this month. But you are still very welcome to contribute your effort on coding or high-level comments.

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

### Running instructions
The tutorial of how to use Muskits is at https://github.com/SJTMusicTeam/Muskits/blob/main/doc/tutorial.md

