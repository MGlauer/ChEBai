from chebai.result.base import ResultProcessor
import json


class JSONResultProcessor(ResultProcessor):

    @classmethod
    def _identifier(cls):
        return "json"

    def start(self):
        self.data = []

    def close(self):
        with open("predictions.json", "w") as fout:
            json.dump(self.data, fout)
            del self.data

    def process_prediction(self, raw_features, raw_labels, features, labels, pred):
        self.data.append(dict(smiles=raw_features, labels=labels.tolist(), prediction=pred["logits"].tolist()))

