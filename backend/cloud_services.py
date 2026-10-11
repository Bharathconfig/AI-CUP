import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from .config import Settings
from .models import Snapshot

logger = logging.getLogger(__name__)


class CloudServices:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bigquery = None
        self.storage = None
        self.publisher = None
        self.vision = None

    async def record(self, snapshot: Snapshot):
        if not any((self.settings.bigquery_table, self.settings.storage_bucket,
                    self.settings.pubsub_topic)):
            return
        payload = snapshot.model_dump(mode="json")
        tasks = []
        if self.settings.bigquery_table:
            tasks.append(self._best_effort(self._write_bigquery, payload))
        if self.settings.storage_bucket:
            tasks.append(self._best_effort(self._archive_snapshot, payload))
        if self.settings.pubsub_topic:
            tasks.append(self._best_effort(self._publish_event, payload))
        await asyncio.gather(*tasks)

    async def _best_effort(self, operation, payload):
        try:
            await asyncio.wait_for(asyncio.to_thread(operation, payload), timeout=10)
        except Exception:
            logger.warning("Optional Google Cloud integration unavailable")

    def _write_bigquery(self, payload):
        if self.bigquery is None:
            from google.cloud import bigquery
            self.bigquery = bigquery.Client(project=self.settings.project)
        revision = payload["state"]["revision"]
        errors = self.bigquery.insert_rows_json(
            self.settings.bigquery_table,
            [{"revision": revision, "event": payload["state"]["event"],
              "updated_at": payload["state"]["updated_at"],
              "generation_id": payload["state"]["generation_id"],
              "snapshot_json": json.dumps(payload, separators=(",", ":"))}],
            row_ids=[f"{payload['state']['generation_id']}-{revision}"],
        )
        if errors:
            raise RuntimeError("BigQuery insert failed")

    def _archive_snapshot(self, payload):
        if self.storage is None:
            from google.cloud import storage
            self.storage = storage.Client(project=self.settings.project)
        state = payload["state"]
        blob = self.storage.bucket(self.settings.storage_bucket).blob(
            f"snapshots/{state['generation_id']}/{state['revision']}.json"
        )
        blob.upload_from_string(json.dumps(payload), content_type="application/json")

    def _publish_event(self, payload):
        if self.publisher is None:
            from google.cloud import pubsub_v1
            self.publisher = pubsub_v1.PublisherClient()
        topic = self.publisher.topic_path(self.settings.project, self.settings.pubsub_topic)
        state = payload["state"]
        self.publisher.publish(topic, json.dumps({
            "generation_id": state["generation_id"],
            "revision": state["revision"],
            "event": state["event"],
            "updated_at": state["updated_at"],
        }).encode()).result(timeout=8)

    async def analyze_image(self, gcs_uri: str):
        if not self.settings.storage_bucket:
            raise RuntimeError("Image analysis is not configured")
        expected_prefix = f"gs://{self.settings.storage_bucket}/"
        if not gcs_uri.startswith(expected_prefix):
            raise ValueError("Image must be stored in the configured Cloud Storage bucket")
        return await asyncio.to_thread(self._analyze_image, gcs_uri)

    def _analyze_image(self, gcs_uri):
        from google.cloud import vision
        if self.vision is None:
            self.vision = vision.ImageAnnotatorClient()
        response = self.vision.label_detection(
            image=vision.Image(source=vision.ImageSource(image_uri=gcs_uri))
        )
        if response.error.message:
            raise RuntimeError("Vision API image analysis failed")
        return [{"description": label.description, "score": label.score}
                for label in response.label_annotations]

    async def solar_context(self, latitude: float, longitude: float):
        if not self.settings.earth_engine_project:
            raise RuntimeError("Earth Engine is not configured")
        return await asyncio.to_thread(self._solar_context, latitude, longitude)

    def _solar_context(self, latitude: float, longitude: float):
        import ee
        ee.Initialize(project=self.settings.earth_engine_project)
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=7)
        irradiance = (
            ee.ImageCollection("ECMWF/ERA5/HOURLY")
            .filterDate(start.isoformat(), end.isoformat())
            .select("surface_solar_radiation_downwards")
            .mean()
            .reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=ee.Geometry.Point([longitude, latitude]),
                scale=27830,
                maxPixels=1,
            )
            .getInfo()
        )
        value = irradiance.get("surface_solar_radiation_downwards")
        return {
            "dataset": "ECMWF/ERA5/HOURLY",
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "mean_hourly_irradiance_kwh_m2": value / 3_600_000 if value is not None else None,
        }
