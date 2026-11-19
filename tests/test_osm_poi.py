import unittest
from unittest.mock import Mock, patch

from osm_poi import (
    classify_idle_context,
    nearby_lodging_feature,
    nearby_osm_features,
    ordinary_stop_feature,
)


def feature(kind, distance, rover_dog=False):
    return {
        "name": f"Test {kind}",
        "kind": kind,
        "distance_m": distance,
        "rover_dog": rover_dog,
    }


class TestPOIClassification(unittest.TestCase):

    def test_fuel_stop(self):
        result, _ = classify_idle_context(
            [feature("fuel", 50, True)]
        )
        self.assertEqual(result, "ROVER_DOG")

    def test_lodging(self):
        result, _ = classify_idle_context(
            [feature("hotel", 80)]
        )
        self.assertEqual(result, "QUIET_TIME")

    def test_food_stop(self):
        result, _ = classify_idle_context(
            [feature("restaurant", 75)]
        )
        self.assertEqual(result, "FOOD_STOP")

    def test_recreational_stop(self):
        result, _ = classify_idle_context(
            [feature("park", 200)]
        )
        self.assertEqual(result, "ROVER_BREAK")

    def test_no_features(self):
        result, _ = classify_idle_context([])
        self.assertEqual(result, "POSSIBLE_ACTIVATION")

    def test_lookup_failure(self):
        self.assertEqual(classify_idle_context(None), (None, None))

    def test_ordinary_stop(self):
        result = ordinary_stop_feature(
            [feature("fuel", 50, True)]
        )
        self.assertIsNotNone(result)

    def test_lodging_lookup(self):
        result = nearby_lodging_feature(
            [feature("hotel", 80)]
        )
        self.assertIsNotNone(result)


class TestOverpassProcessing(unittest.TestCase):

    @patch("osm_poi.requests.post")
    def test_feature_processing(self, mock_post):
        sample_data = {
            "elements": [
                {
                    "type": "node",
                    "id": 101,
                    "lat": 44.9554,
                    "lon": -93.1022,
                    "tags": {"amenity": "fuel", "name": "Test Fuel"},
                },
                {
                    "type": "node",
                    "id": 102,
                    "lat": 44.9558,
                    "lon": -93.1022,
                    "tags": {
                        "amenity": "restaurant",
                        "name": "Test Restaurant",
                    },
                },
                {
                    "type": "node",
                    "id": 103,
                    "lat": 44.9600,
                    "lon": -93.1022,
                    "tags": {"tourism": "hotel", "name": "Distant Hotel"},
                },
            ]
        }

        response = Mock()
        response.json.return_value = sample_data
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        features = nearby_osm_features(44.9553, -93.1022)

        self.assertEqual(len(features), 2)
        self.assertEqual(features[0]["kind"], "fuel")
        self.assertTrue(features[0]["rover_dog"])
        self.assertEqual(features[1]["kind"], "restaurant")
        self.assertLess(
            features[0]["distance_m"],
            features[1]["distance_m"],
        )


if __name__ == "__main__":
    unittest.main()
