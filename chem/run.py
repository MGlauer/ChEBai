from chem.models import graph, recursive, chemyk
from chem import data
import sys

def main(batch_size):
    exps = [
        #(models.lstm.ChemLSTM, [100, 500, 500], (data.JCIExtendedData, data.JCIData)),
        #(models.graph.JCIGraphNet, [100, 100, 500], (data.JCIGraphData, data.JCIExtendedGraphData)),
        #(graph.JCIGraphAttentionNet, [100, 100, 500], (data.JCIGraphData, data.JCIExtendedGraphData)),
        (chemyk.ChemYK, [50, 50, 500], (data.JCITokenData,)),
        #(models.graph_k2.JCIGraphK2Net, [100, 100, 500], (data.JCIGraphTwoData, data.JCIExtendedGraphTwoData))
    ]
    for net_cls, model_args, datasets in exps:
        for dataset in datasets:
            for weighted in [False]:
                net_cls.run(dataset(batch_size), net_cls.NAME, model_args=model_args, weighted=weighted)

if __name__ == "__main__":
    main(int(sys.argv[1]))