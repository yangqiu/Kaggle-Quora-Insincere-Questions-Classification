import gensim
import torch
import numpy as np
from torch import nn

from qiqc.builder import build_aggregator
from qiqc.embeddings import load_pretrained_vectors
from qiqc.models import Word2VecEx
from qiqc.models import WordEmbedding
from qiqc.models import BinaryClassifier
from qiqc.models import AverageEnsembler


def build_sampler(epoch, weights):
    if epoch % 2 == 0:
        sampler = torch.utils.data.WeightedRandomSampler(
            weights=weights, num_samples=len(weights), replacement=True)
    else:
        sampler = None
    return sampler


def build_embedding(config, tokens):
    vocab = gensim.models.word2vec.Word2VecVocab()
    vocab.scan_vocab(tokens)
    token2id = dict([(k, i + 2) for i, (k, v) in enumerate(sorted(
        vocab.raw_vocab.items(), key=lambda x:x[1], reverse=True))])
    token2id = dict(**{'<PAD>': 0, '<UNK>': 1}, **token2id)
    pretrained_vectors = load_pretrained_vectors(
        config['embedding']['src'], token2id, test=config['test'])

    embedding_matrices = []
    for name, vec in pretrained_vectors.items():
        model = Word2VecEx(**config['embedding']['params'])
        model.build_vocab_from_freq(vocab.raw_vocab)
        model.initialize_pretrained_vector(vec)
        embedding_matrices.append(
            model.build_embedding_matrix(
                token2id, standardize=config['embedding']['standardize']))
    embedding_matrix = np.array(embedding_matrices).mean(axis=0)

    return token2id, embedding_matrix


class Encoder(nn.Module):

    def __init__(self, config, embedding_matrix):
        super().__init__()
        self.embed = WordEmbedding(
            *embedding_matrix.shape,
            n_hidden=config['embed']['n_hidden'],
            freeze_embed=config['embed']['freeze_embed'],
            pretrained_vectors=embedding_matrix,
            position=config['embed']['position'],
            hidden_bn=config['embed']['hidden_bn'],
            dropout=config['embed']['dropout'],
        )
        self.encoder = nn.LSTM(
            input_size=self.embed.out_dim,
            hidden_size=config['encoder']['n_hidden'],
            num_layers=config['encoder']['n_layers'],
            dropout=config['encoder']['dropout'],
            bidirectional=True,
            batch_first=True,
        )
        self.aggregator = build_aggregator(
            config['encoder']['aggregator'],
        )

    def forward(self, X, mask):
        h = self.embed(X)
        h, _ = self.encoder(h)
        h = self.aggregator(h, mask)
        return h


def build_model(config, embedding_matrix):
    encoder = Encoder(config['model'], embedding_matrix)
    clf = BinaryClassifier(config['model'], encoder)
    return clf


def build_optimizer(config, model):
    optimizer = torch.optim.Adam(
        model.parameters(), lr=float(config['optimizer']['lr']))
    # optimizer = torch.optim.SGD(
    #     model.parameters(), lr=float(config['optimizer']['lr']))
    return optimizer


def build_ensembler(*args, **kwargs):
    return AverageEnsembler(*args, **kwargs)