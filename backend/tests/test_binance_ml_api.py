import unittest
from unittest.mock import patch

from backend import app as app_module


class BinanceMachineLearningApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_model_status_returns_the_local_model_contract(self):
        expected = {"network": "mainnet", "available": False, "latestModel": None}
        with patch.object(app_module, "get_binance_ml_status", return_value=expected) as status:
            response = self.client.get("/api/binance/futures/model/status?network=mainnet")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": expected})
        status.assert_called_once_with("mainnet")

    def test_model_training_starts_a_local_background_job(self):
        expected = {"id": "ml-test", "status": "QUEUED"}
        config = {"maxTrainSamples": 1000, "epochs": 1}
        with patch.object(app_module, "start_binance_ml_training", return_value=expected) as start:
            response = self.client.post("/api/binance/futures/model/train", json={"network": "mainnet", "config": config})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"job": expected})
        start.assert_called_once_with("mainnet", config)

    def test_model_training_status_returns_persisted_or_active_job(self):
        expected = {"id": "ml-test", "status": "RUNNING", "progress": {"phase": "DATASET"}}
        with patch.object(app_module, "get_binance_ml_training_job", return_value=expected) as status:
            response = self.client.get("/api/binance/futures/model/train/ml-test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"job": expected})
        status.assert_called_once_with("ml-test")

    def test_model_runs_returns_saved_checkpoint_catalog(self):
        expected = {"network": "mainnet", "models": [{"runId": "ml-test", "branch": "BEST"}]}
        with patch.object(app_module, "list_binance_ml_models", return_value=expected) as catalog:
            response = self.client.get("/api/binance/futures/model/runs?network=mainnet&limit=8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        catalog.assert_called_once_with("mainnet", "8")


if __name__ == "__main__":
    unittest.main()
