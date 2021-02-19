import fastobo
import networkx as nx
import pickle
import os
import sys
from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score
import torch
from torch import nn
from torch.utils.data import random_split
import requests
from functools import lru_cache
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.metrics import F1
from pytorch_lightning import loggers as pl_loggers
import pysmiles as ps
import torch.nn.functional as F
import random


import multiprocessing as mp
from torch_geometric import nn as tgnn
from torch_geometric.utils.convert import from_networkx
from torch_geometric.utils import train_test_split_edges
from torch_geometric.data import InMemoryDataset, Data, DataLoader

import logging
logging.getLogger('pysmiles').setLevel(logging.CRITICAL)


class PrePairData(Data):
    def __init__(self, l=None, r=None, label=None):
        super(PrePairData, self).__init__()
        self.l = l
        self.r = r
        self.label = label


class PairData(Data):
    def __init__(self, ppd: PrePairData, graph):
        super(PairData, self).__init__()

        s = graph.nodes[ppd.l.item()]["enc"]
        self.edge_index_s = s.edge_index
        self.x_s = s.x

        t = graph.nodes[ppd.r.item()]["enc"]
        self.edge_index_t = t.edge_index
        self.x_t = t.x

        self.label = ppd.label

    def __inc__(self, key, value):
        if key == 'edge_index_s':
            return self.x_s.size(0)
        if key == 'edge_index_t':
            return self.x_t.size(0)
        else:
            return super().__inc__(key, value)


class PartOfData(InMemoryDataset):

    def transform(self, ppd: PrePairData):
        return PairData(ppd, self.graph)

    def __init__(self, root, kind="train", train_split=0.8, part_split=0.1, pre_transform=None, **kwargs):
        self.cache_file = ".part_data.pkl"
        self._ignore = set()
        self.train_split = train_split
        self.part_split = part_split
        super().__init__(root, self.transform, pre_transform, **kwargs)
        self.data = {}
        self.slices = {}
        self.data, self.slices = torch.load(os.path.join(self.processed_dir, f"{kind}.pt"))
        self.graph = torch.load(os.path.join(self.processed_dir, self.processed_cache_names[0]))

    def download(self):
        url = 'http://purl.obolibrary.org/obo/chebi.obo'
        r = requests.get(url, allow_redirects=True)
        open(self.raw_paths[0], 'wb').write(r.content)

    def process(self):
        doc = fastobo.load(self.raw_paths[0])
        elements = list()
        for clause in doc:
            callback = CALLBACKS.get(type(clause))
            if callback is not None:
                elements.append(callback(clause))

        g = nx.DiGraph()
        for n in elements:
            g.add_node(n["id"], **n)
        g.add_edges_from([(p, q["id"]) for q in elements for p in q["parents"]])

        print("pass parts")
        self.pass_parts(g, 23367, set())
        print("Load data")
        children = list(nx.single_source_shortest_path(g, 23367).keys())[:100]
        parts = list({p for c in children for p in g.nodes[c]["has_part"]})
        print("Create molecules")
        with mp.Pool() as p:
            nx.set_node_attributes(g, dict(p.imap_unordered(get_mol_enc,((i,g.nodes[i]["smiles"]) for i in (children + parts)))), "enc")

        print("Filter invalid structures")
        children = [p for p in children if g.nodes[p]["enc"]]
        random.shuffle(children)
        children_test_only = children[:int(len(children)*self.part_split)]
        children = children[int(len(children) * self.part_split):]

        parts = [p for p in parts if g.nodes[p]["enc"]]
        random.shuffle(parts)
        parts_test_only = parts[:int(len(parts)*self.part_split)]
        parts = parts[int(len(parts)*self.part_split):]

        print("Transform into torch structure")
        ppds = [PrePairData(l, r, float(r in g.nodes[l]["has_part"])) for l in children for r in parts]
        ppds_test_only = [PrePairData(l, r, float(r in g.nodes[l]["has_part"])) for l in children for r in parts_test_only] + [PrePairData(l, r, float(r in g.nodes[l]["has_part"])) for l in children_test_only for r in parts_test_only] + [PrePairData(l, r, float(r in g.nodes[l]["has_part"])) for l in children_test_only for r in parts]

        lp = len(ppds)
        splits = {
            "train":(0,int(lp*self.train_split)),
            "validation":(int(lp*self.train_split),int(lp*self.train_split)+int(lp*(self.train_split**2))),
            "test":(int(lp*self.train_split)+int(lp*(self.train_split**2)),-1)
        }
        for kind, (l,r) in splits.items():
            print("Save", kind)
            d = ppds[l:r]
            if kind == "test":
                d += ppds_test_only
            data, slices = self.collate(d)
            torch.save((data, slices), os.path.join(self.processed_dir, f"{kind}.pt"))
        torch.save(g, os.path.join(self.processed_dir, self.processed_cache_names[0]))

    @property
    def raw_file_names(self):
        return ["chebi.obo"]

    @property
    def processed_file_names(self):
        return ["train.pt", "test.pt", "validation.pt"]

    @property
    def processed_cache_names(self):
        return ["cache.pt"]

    def pass_parts(self, d: nx.DiGraph, root, parts=None):
        if parts is None:
            parts = set()
        parts = set(parts.union(d.nodes[root]["has_part"]))
        nx.set_node_attributes(d, {root: parts}, "has_part")
        for child in d.successors(root):
            self.pass_parts(d, child, set(parts))

    def extract_children(self, d: nx.DiGraph, root, part_cache):
        smiles = d.nodes[root]["smiles"]
        if smiles:
            yield root
        for child in d.successors(root):
            for r in self.extract_children(d, child, part_cache):
                yield r

def chebi_to_int(s):
    return int(s[s.index(":")+1:])

def term_callback(doc):
    parts = set()
    parents = []
    name = None
    smiles = None
    for clause in doc:
        if isinstance(clause, fastobo.term.PropertyValueClause):
            t = clause.property_value
            if str(t.relation) == "http://purl.obolibrary.org/obo/chebi/smiles":
                smiles = t.value
        elif isinstance(clause, fastobo.term.RelationshipClause):
            if str(clause.typedef) == "has_part":
                parts.add(chebi_to_int(str(clause.term)))
        elif isinstance(clause, fastobo.term.IsAClause):
            parents.append(chebi_to_int(str(clause.term)))
        elif isinstance(clause, fastobo.term.NameClause):
            name = str(clause.name)
    return {
        "id": chebi_to_int(str(doc.id)),
        "parents": parents,
        "has_part": parts,
        "name": name,
        "smiles": smiles,
    }


CALLBACKS = {
    fastobo.term.TermFrame: term_callback,
}


class PartOfNet(pl.LightningModule):

    def __init__(self, in_length, loops=10):
        super().__init__()
        self.loops=loops
        self.left_graph_net = tgnn.GATConv(in_length, in_length)
        self.right_graph_net = tgnn.GATConv(in_length, in_length)
        self.attention = nn.Linear(in_length, 1)
        self.global_attention = tgnn.GlobalAttention(self.attention)
        self.output_net = nn.Sequential(nn.Linear(2*in_length,2*in_length), nn.Linear(2*in_length,in_length), nn.Linear(in_length,1))
        self.f1 = F1(1, average="micro", threshold=0.5)

    def _execute(self, batch, batch_idx):
        pred = self(batch).squeeze(1)
        loss = F.binary_cross_entropy_with_logits(pred, batch.label)
        f1 = self.f1(batch.label > 0.5, torch.sigmoid(pred) > 0.5)
        return loss, f1

    def training_step(self, *args, **kwargs):
        loss, f1 = self._execute(*args, **kwargs)
        self.log('train_loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log('train_f1', f1, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        return loss

    def validation_step(self, *args, **kwargs):
        with torch.no_grad():
            loss, f1 = self._execute(*args, **kwargs)
            self.log('val_loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
            self.log('val_f1', f1, on_step=True, on_epoch=True, prog_bar=True, logger=True)
            return loss

    def forward(self, x):
        a = self.left_graph_net(x.x_s, x.edge_index_s.long())
        b = self.right_graph_net(x.x_t, x.edge_index_t.long())
        return self.output_net(torch.cat([self.global_attention(a, x.x_s_batch),self.global_attention(b,x.x_t_batch)], dim=1))

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters())
        return optimizer


def get_mol_enc(x):
    i, s= x
    return i, mol_to_data(s) if s else None


def mol_to_data(smiles):
    try:
        mol = ps.read_smiles(smiles)
    except:
        return None
    d = {}
    for node in mol.nodes:
        el = mol.nodes[node].get("element")
        if el is not None:
            v = atom_index.index(el)
            base = [float(i == v) for i in range(118)]
            wildcard = [0.0]
        else:
            base = [0.0 for i in range(118)]
            wildcard = [1.0]
        d[node] = base + [mol.nodes[node].get("charge",0.0), mol.nodes[node].get("hcount",0.0)] + wildcard

        for attr in list(mol.nodes[node].keys()):
            del mol.nodes[node][attr]
    nx.set_node_attributes(mol, d, "x")
    return from_networkx(mol)

atom_index =(
            "\*",
            "H",
            "He",
            "Li",
            "Be",
            "B",
            "C",
            "N",
            "O",
            "F",
            "Ne",
            "Na",
            "Mg",
            "Al",
            "Si",
            "P",
            "S",
            "Cl",
            "Ar",
            "K",
            "Ca",
            "Sc",
            "Ti",
            "V",
            "Cr",
            "Mn",
            "Fe",
            "Co",
            "Ni",
            "Cu",
            "Zn",
            "Ga",
            "Ge",
            "As",
            "Se",
            "Br",
            "Kr",
            "Rb",
            "Sr",
            "Y",
            "Zr",
            "Nb",
            "Mo",
            "Tc",
            "Ru",
            "Rh",
            "Pd",
            "Ag",
            "Cd",
            "In",
            "Sn",
            "Sb",
            "Te",
            "I",
            "Xe",
            "Cs",
            "Ba",
            "La",
            "Hf",
            "Ta",
            "W",
            "Re",
            "Os",
            "Ir",
            "Pt",
            "Au",
            "Hg",
            "Tl",
            "Pb",
            "Bi",
            "Po",
            "At",
            "Rn",
            "Fr",
            "Ra",
            "Ac",
            "Rf",
            "Db",
            "Sg",
            "Bh",
            "Hs",
            "Mt",
            "Ds",
            "Rg",
            "Cn",
            "Nh",
            "Fl",
            "Mc",
            "Lv",
            "Ts",
            "Og",
            "Ce",
            "Pr",
            "Nd",
            "Pm",
            "Sm",
            "Eu",
            "Gd",
            "Tb",
            "Dy",
            "Ho",
            "Er",
            "Tm",
            "Yb",
            "Lu",
            "Th",
            "Pa",
            "U",
            "Np",
            "Pu",
            "Am",
            "Cm",
            "Bk",
            "Cf",
            "Es",
            "Fm",
            "Md",
            "No",
            "Lr",
            "c",
            "n",
            "s",
            "o",
            "se",
            "p",
)

def train(train_loader, validation_loader):
    if torch.cuda.is_available():
        trainer_kwargs = dict(gpus=-1, accelerator="ddp")
    else:
        trainer_kwargs = dict(gpus=0)
    net = PartOfNet(121)
    tb_logger = pl_loggers.CSVLogger('logs/')
    checkpoint_callback = ModelCheckpoint(
        dirpath=os.path.join(tb_logger.log_dir, "checkpoints"),
        filename="{epoch}-{step}-{val_loss:.7f}",
        save_top_k=5,
        save_last=True,
        verbose=True,
        monitor='val_loss',
        mode='min'
    )
    trainer = pl.Trainer(max_epochs=2, logger=tb_logger, callbacks=[checkpoint_callback], replace_sampler_ddp=False, log_every_n_steps=10, val_check_interval=0.1 ,**trainer_kwargs)
    trainer.fit(net, train_loader, val_dataloaders=validation_loader)


if __name__ == "__main__":
    train_loader = DataLoader(PartOfData(".", kind="train"), shuffle = True, batch_size = int(
        sys.argv[1]), follow_batch = ["x_s", "x_t", "edge_index_s",
                                      "edge_index_t"])

    validation_loader = DataLoader(PartOfData(".", kind="validation"), shuffle = True, batch_size = int(
        sys.argv[1]), follow_batch = ["x_s", "x_t", "edge_index_s",
                                      "edge_index_t"])

    train(train_loader, validation_loader)
