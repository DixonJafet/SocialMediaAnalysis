#!/usr/bin/env python
"""Classify MongoDB comments and publication captions with Azure AI Foundry."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError


DATABASE_DEFAULT = "SocialMediaDB"
COLLECTION_NAME = "SocialMediaDB"
FOUNDRY_ENDPOINT_DEFAULT = (
    "https://jafetdixon-3562-resource.cognitiveservices.azure.com"
)
FOUNDRY_DEPLOYMENT_DEFAULT = "gpt-5-mini-1"
COMMENT_BATCH_SIZE = 40
CAPTION_BATCH_SIZE = 20
MAX_WORKERS = 3
MAX_RETRIES = 5

PLATFORMS = ("facebook", "instagram")
SENTIMENTS = ("positivo", "negativo", "neutro")
NEGATIVE_TONES = frozenset(
    ("sarcasmo", "burla", "enojo", "cuestionamientos", "critica")
)
POSITIVE_TONES = frozenset(("apoyo", "felicidad", "esperanza"))
TONES = tuple(sorted(NEGATIVE_TONES | POSITIVE_TONES))
CATEGORIES = (
    "Transporte",
    "Comercio",
    "Educacion",
    "Diplomacia",
    "Seguridad",
    "Salud",
    "Agropecuario",
    "Cultura",
    "Deporte",
    "Tecnologia",
    "Justicia",
    "Ambiente",
    "Economia",
    "Trabajo",
    "Vivienda",
    "Energia",
    "Hacienda",
    "Turismo",
    "Gobierno",
    "Planificacion",
)

ENV_KEYS = frozenset(
    (
        "MONGODB_URI",
        "MONGODB_DB",
        "FOUNDRY_ENDPOINT",
        "FOUNDRY_DEPLOYMENT",
    )
)

COMMENT_SYSTEM_PROMPT = (
    "Clasifica cada comentario en español usando también la descripción de su "
    "publicación como contexto. Asigna un único sentimiento: positivo, negativo "
    "o neutro. Si es ambiguo o principalmente informativo, usa neutro y tono []. "
    "Para un sentimiento positivo elige exactamente un tono entre apoyo, "
    "felicidad y esperanza. Para uno negativo elige exactamente un tono entre "
    "sarcasmo, burla, enojo, cuestionamientos y critica. El tono debe reflejar "
    "claramente el texto; no confundas una pregunta genuina con sarcasmo. "
    "Devuelve solo el objeto JSON solicitado, sin explicaciones."
)
CATEGORY_SYSTEM_PROMPT = (
    "Clasifica cada descripción de publicación por su rubro principal. Elige "
    "exactamente una opción de la lista permitida. Considera Ambiente, Energia, "
    "Hacienda, Economia, Trabajo, Vivienda, Turismo, Gobierno y Planificacion "
    "como rubros adicionales relacionados con ministerios de Costa Rica cuando "
    "correspondan. Devuelve solo el objeto JSON solicitado, sin explicaciones."
)

COMMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "sentimiento": {"type": "string", "enum": list(SENTIMENTS)},
                    "tono": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(TONES)},
                        "maxItems": 1,
                    },
                },
                "required": ["id", "sentimiento", "tono"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}
CATEGORY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "rubro": {"type": "string", "enum": list(CATEGORIES)},
                },
                "required": ["id", "rubro"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def read_env_file(path: Path) -> dict[str, str]:
    """Read only the pipeline's supported keys from a simple KEY=VALUE file."""
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_config(project_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for filename in ("atlas-credentials.env", ".env"):
        values.update(read_env_file(project_root / filename))

    for key in ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]

    mongodb_uri = values.get("MONGODB_URI", "").strip()
    if not mongodb_uri:
        raise RuntimeError(
            "MONGODB_URI is missing. Set it in .env or atlas-credentials.env."
        )

    endpoint = (
        values.get("FOUNDRY_ENDPOINT", "").strip()
        or FOUNDRY_ENDPOINT_DEFAULT
    ).rstrip("/")
    if not endpoint.startswith("https://"):
        raise RuntimeError("FOUNDRY_ENDPOINT must use HTTPS.")

    return {
        "MONGODB_URI": mongodb_uri,
        "MONGODB_DB": values.get("MONGODB_DB", "").strip() or DATABASE_DEFAULT,
        "FOUNDRY_ENDPOINT": endpoint,
        "FOUNDRY_DEPLOYMENT": values.get(
            "FOUNDRY_DEPLOYMENT", FOUNDRY_DEPLOYMENT_DEFAULT
        ).strip()
        or FOUNDRY_DEPLOYMENT_DEFAULT,
    }


def is_valid_comment_classification(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    sentiment = value.get("sentimiento")
    tone = value.get("tono")
    if sentiment not in SENTIMENTS or not isinstance(tone, list) or len(tone) > 1:
        return False
    if not all(isinstance(item, str) for item in tone):
        return False
    if sentiment == "neutro":
        return not tone
    if sentiment == "positivo":
        return len(tone) == 1 and tone[0] in POSITIVE_TONES
    return len(tone) == 1 and tone[0] in NEGATIVE_TONES


def is_valid_comment_labels(comments: Any, labels: Any) -> bool:
    if not isinstance(comments, list) or not isinstance(labels, list):
        return False
    if len(comments) != len(labels):
        return False
    for index, label in enumerate(labels):
        if not isinstance(label, dict) or type(label.get("index")) is not int:
            return False
        if label["index"] != index or not is_valid_comment_classification(label):
            return False
    return True


def get_azure_access_token() -> str:
    command = (
        "az account get-access-token --resource "
        "https://cognitiveservices.azure.com/ --query accessToken -o tsv"
    )
    try:
        result = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(
            "Could not run Azure CLI. Install Azure CLI and run az login."
        ) from error
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "Azure CLI could not provide a Foundry token. Run az login and "
            "select the subscription that owns the Foundry resource."
        )
    return result.stdout.strip()


class AzureTokenProvider:
    def __init__(self) -> None:
        self._token = get_azure_access_token()
        self._lock = threading.Lock()

    def current(self) -> str:
        with self._lock:
            return self._token

    def refresh_if_current(self, previous: str) -> str:
        with self._lock:
            if self._token == previous:
                self._token = get_azure_access_token()
            return self._token


class FoundryClient:
    def __init__(self, endpoint: str, deployment: str) -> None:
        self.endpoint = endpoint.rstrip("/") + "/openai/v1/chat/completions"
        self.deployment = deployment
        self.tokens = AzureTokenProvider()

    def complete_json(
        self,
        schema_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        schema: dict[str, Any],
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        payload = {
            "model": self.deployment,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        user_payload, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            ],
            "reasoning_effort": "low",
            "verbosity": "low",
            "max_completion_tokens": max_completion_tokens,
            "store": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        refreshed = False

        for attempt in range(MAX_RETRIES + 1):
            token = self.tokens.current()
            request = urllib.request.Request(
                self.endpoint,
                data=request_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    body = json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 401 and not refreshed:
                    self.tokens.refresh_if_current(token)
                    refreshed = True
                    continue
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    raise RuntimeError(
                        f"Foundry request failed with HTTP {error.code}."
                    ) from None
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Foundry request exhausted retries (HTTP {error.code})."
                    ) from None
                delay = min(2**attempt, 30) + random.random()
                print(
                    f"Retrying Foundry request after HTTP {error.code} "
                    f"({delay:.1f}s).",
                    flush=True,
                )
                time.sleep(delay)
                continue
            except urllib.error.URLError as error:
                if attempt == MAX_RETRIES:
                    reason = type(error.reason).__name__
                    raise RuntimeError(
                        f"Foundry request exhausted retries ({reason})."
                    ) from None
                delay = min(2**attempt, 30) + random.random()
                print(
                    f"Retrying Foundry request after a network error "
                    f"({delay:.1f}s).",
                    flush=True,
                )
                time.sleep(delay)
                continue

            choices = body.get("choices") or []
            if not choices:
                raise RuntimeError("Foundry response contained no choices.")
            choice = choices[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if message.get("refusal"):
                raise RuntimeError("Foundry refused a labeling request.")
            if choice.get("finish_reason") != "stop" or not content:
                raise RuntimeError("Foundry returned an incomplete response.")
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                raise RuntimeError("Foundry returned invalid JSON.") from None
            if not isinstance(result, dict):
                raise RuntimeError("Foundry JSON response must be an object.")
            return result

        raise RuntimeError("Foundry request failed after retries.")


def validate_response_ids(
    response: dict[str, Any], expected_ids: set[str], kind: str
) -> list[dict[str, Any]]:
    items = response.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"Foundry {kind} response is missing its items array.")
    returned_ids = [
        item.get("id")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if (
        len(returned_ids) != len(items)
        or len(returned_ids) != len(expected_ids)
        or set(returned_ids) != expected_ids
        or len(set(returned_ids)) != len(returned_ids)
    ):
        raise RuntimeError(f"Foundry {kind} response IDs do not match the request.")
    return items


def classify_comment_batch(
    client: FoundryClient, batch: list[dict[str, str]]
) -> dict[str, dict[str, Any]]:
    contexts: dict[str, str] = {}
    for item in batch:
        contexts.setdefault(item["context_id"], item["context"])
    payload = {
        "contexts": [
            {"id": context_id, "description": text}
            for context_id, text in contexts.items()
        ],
        "comments": [
            {
                "id": item["id"],
                "context_id": item["context_id"],
                "text": item["text"],
            }
            for item in batch
        ],
    }
    expected_ids = {item["id"] for item in batch}

    for attempt in range(2):
        system_prompt = COMMENT_SYSTEM_PROMPT
        if attempt:
            system_prompt += (
                " Revisa que neutro tenga tono vacío y que cada comentario "
                "positivo o negativo tenga exactamente un tono de su grupo."
            )
        response = client.complete_json(
            "comment_labels",
            system_prompt,
            payload,
            COMMENT_SCHEMA,
            max_completion_tokens=4096,
        )
        items = validate_response_ids(response, expected_ids, "comment")
        labels: dict[str, dict[str, Any]] = {}
        invalid = False
        for item in items:
            value = {
                "sentimiento": item.get("sentimiento"),
                "tono": item.get("tono"),
            }
            if not is_valid_comment_classification(value):
                invalid = True
                break
            labels[item["id"]] = value
        if not invalid:
            return labels
    raise RuntimeError("Foundry returned invalid comment labels twice.")


def classify_caption_batch(
    client: FoundryClient, batch: list[dict[str, str]]
) -> dict[str, str]:
    response = client.complete_json(
        "caption_categories",
        CATEGORY_SYSTEM_PROMPT,
        {"items": batch},
        CATEGORY_SCHEMA,
        max_completion_tokens=2048,
    )
    items = validate_response_ids(
        response, {item["id"] for item in batch}, "caption"
    )
    result: dict[str, str] = {}
    for item in items:
        category = item.get("rubro")
        if category not in CATEGORIES:
            raise RuntimeError("Foundry returned an unsupported publication category.")
        result[item["id"]] = category
    return result


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def run_batches(
    kind: str,
    items: list[dict[str, Any]],
    batch_size: int,
    results: dict[str, Any],
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    client: FoundryClient,
) -> None:
    pending = [item for item in items if item["id"] not in results]
    batches = chunks(pending, batch_size)
    print(f"{kind}: {len(pending)} inputs in {len(batches)} batches.", flush=True)
    if not batches:
        return

    classifier = (
        classify_comment_batch if kind == "comments" else classify_caption_batch
    )
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(classifier, client, batch): batch for batch in batches
        }
        for future in concurrent.futures.as_completed(futures):
            results.update(future.result())
            save_checkpoint(checkpoint_path, checkpoint)
            completed += 1
            if completed % 10 == 0 or completed == len(batches):
                print(
                    f"{kind.capitalize()} progress: {completed}/{len(batches)} batches.",
                    flush=True,
                )


def save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(checkpoint, file, ensure_ascii=False, separators=(",", ":"))
    temporary_path.replace(path)


def load_checkpoint(
    path: Path,
    fingerprint: str,
    deployment: str,
    comment_ids: set[str],
    caption_ids: set[str],
) -> dict[str, Any]:
    if not path.exists():
        return {
            "fingerprint": fingerprint,
            "deployment": deployment,
            "comment_labels": {},
            "categories": {},
        }
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Could not read checkpoint {path}: {type(error).__name__}."
        ) from None
    if (
        checkpoint.get("fingerprint") != fingerprint
        or checkpoint.get("deployment") != deployment
    ):
        raise RuntimeError(
            f"Checkpoint {path} belongs to different input data or deployment. "
            "Remove it or pass a different --checkpoint path."
        )
    comments = checkpoint.get("comment_labels")
    categories = checkpoint.get("categories")
    if not isinstance(comments, dict) or not isinstance(categories, dict):
        raise RuntimeError(f"Checkpoint {path} has an invalid structure.")
    if not set(comments).issubset(comment_ids) or not set(categories).issubset(
        caption_ids
    ):
        raise RuntimeError(f"Checkpoint {path} contains unexpected labels.")
    if not all(is_valid_comment_classification(value) for value in comments.values()):
        raise RuntimeError(f"Checkpoint {path} contains invalid comment labels.")
    if not all(value in CATEGORIES for value in categories.values()):
        raise RuntimeError(f"Checkpoint {path} contains invalid categories.")
    return checkpoint


def default_checkpoint_path(database: str, endpoint: str, deployment: str) -> Path:
    identity = f"{database}|{endpoint}|{deployment}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"sm-labeling-{suffix}.json"


def stable_id(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def update_preconditions(record: dict[str, Any]) -> dict[str, Any]:
    query = {"_id": record["_id"], "caption": record["caption"]}
    query["rubro"] = (
        {"$eq": record["rubro"]} if record["has_rubro"] else {"$exists": False}
    )
    for network in PLATFORMS:
        platform = record["platforms"][network]
        query[f"{network}.comments"] = {"$eq": platform["comments"]}
        query[f"{network}.comment_labels"] = (
            {"$eq": platform["labels"]} if platform["has_labels"]
            else {"$exists": False}
        )
    return query


def run_pipeline(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parent.parent
    config = load_config(project_root)
    endpoint = config["FOUNDRY_ENDPOINT"]
    deployment = config["FOUNDRY_DEPLOYMENT"]
    collection_name = COLLECTION_NAME
    mongo_client = MongoClient(
        config["MONGODB_URI"], serverSelectionTimeoutMS=20_000
    )

    try:
        mongo_client.admin.command("ping")
        collection = mongo_client[config["MONGODB_DB"]][collection_name]
        projection = {
            "caption": 1,
            "rubro": 1,
            "facebook.comments": 1,
            "facebook.comment_labels": 1,
            "instagram.comments": 1,
            "instagram.comment_labels": 1,
        }
        documents = list(collection.find({}, projection))
        documents.sort(key=lambda document: stable_id(document["_id"]))
        if not documents:
            raise RuntimeError(
                f"Collection {config['MONGODB_DB']}.{collection_name} is empty."
            )

        records: list[dict[str, Any]] = []
        comment_items: list[dict[str, str]] = []
        caption_items: list[dict[str, str]] = []
        comment_ids_by_pair: dict[tuple[str, str], str] = {}
        caption_ids_by_text: dict[str, str] = {}
        pending_comment_occurrences = 0

        for document in documents:
            caption = document.get("caption")
            if not isinstance(caption, str) or not caption.strip():
                raise RuntimeError(
                    f"Document {stable_id(document['_id'])} has an empty shared caption."
                )
            needs_category = args.relabel or document.get("rubro") not in CATEGORIES
            record = {
                "_id": document["_id"],
                "caption": caption,
                "rubro": document.get("rubro"),
                "has_rubro": "rubro" in document,
                "platforms": {},
                "needs_category": needs_category,
                "caption_id": None,
            }
            if needs_category:
                if caption not in caption_ids_by_text:
                    item_id = f"p{len(caption_ids_by_text):06d}"
                    caption_ids_by_text[caption] = item_id
                    caption_items.append({"id": item_id, "description": caption})
                record["caption_id"] = caption_ids_by_text[caption]
            for network in PLATFORMS:
                platform = document.get(network)
                if not isinstance(platform, dict):
                    raise RuntimeError(
                        f"Document {stable_id(document['_id'])} is missing {network}."
                    )
                comments = platform.get("comments")
                if not isinstance(comments, list) or any(
                    not isinstance(comment, str) for comment in comments
                ):
                    raise RuntimeError(
                        f"Document {stable_id(document['_id'])} has an invalid "
                        f"{network} comments array."
                    )

                needs_comments = args.relabel or not is_valid_comment_labels(
                    comments, platform.get("comment_labels")
                )
                platform_record = {
                    "comments": comments,
                    "labels": platform.get("comment_labels"),
                    "has_labels": "comment_labels" in platform,
                    "needs_comments": needs_comments,
                    "comment_refs": [],
                }
                if needs_comments:
                    pending_comment_occurrences += len(comments)
                    for comment in comments:
                        if not comment.strip():
                            platform_record["comment_refs"].append(None)
                            continue
                        key = (caption, comment)
                        if key not in comment_ids_by_pair:
                            item_id = f"c{len(comment_ids_by_pair):06d}"
                            comment_ids_by_pair[key] = item_id
                            comment_items.append(
                                {
                                    "id": item_id,
                                    "context_id": "",
                                    "context": caption,
                                    "text": comment,
                                }
                            )
                        platform_record["comment_refs"].append(
                            comment_ids_by_pair[key]
                        )

                record["platforms"][network] = platform_record
            records.append(record)

        context_ids_by_text = dict(caption_ids_by_text)
        for item in comment_items:
            caption_id = context_ids_by_text.get(item["context"])
            if caption_id is None:
                caption_id = f"x{len(context_ids_by_text):06d}"
                context_ids_by_text[item["context"]] = caption_id
            item["context_id"] = caption_id

        update_records = [
            record
            for record in records
            if record["needs_category"] or any(
                platform["needs_comments"]
                for platform in record["platforms"].values()
            )
        ]
        if not update_records:
            print(
                f"No work required: all {len(records)} documents already have "
                "valid labels. Use --relabel to classify them again."
            )
            return 0

        fingerprint_payload = {
            "database": config["MONGODB_DB"],
            "collection": collection_name,
            "deployment": deployment,
            "comments": [
                [item["id"], item["context"], item["text"]]
                for item in comment_items
            ],
            "captions": [
                [item["id"], item["description"]] for item in caption_items
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        print(
            f"Documents={len(records)}; documents to update={len(update_records)}; "
            f"comment occurrences={pending_comment_occurrences}; "
            f"unique comments={len(comment_items)}; "
            f"unique descriptions={len(caption_items)}."
        )
        if args.dry_run:
            print("Dry run: no Foundry requests or MongoDB writes were made.")
            return 0

        checkpoint_path = args.checkpoint or default_checkpoint_path(
            config["MONGODB_DB"], endpoint, deployment
        )
        checkpoint = load_checkpoint(
            checkpoint_path,
            fingerprint,
            deployment,
            {item["id"] for item in comment_items},
            {item["id"] for item in caption_items},
        )
        save_checkpoint(checkpoint_path, checkpoint)
        foundry = FoundryClient(endpoint, deployment)
        run_batches(
            "comments",
            comment_items,
            COMMENT_BATCH_SIZE,
            checkpoint["comment_labels"],
            checkpoint,
            checkpoint_path,
            foundry,
        )
        run_batches(
            "descriptions",
            caption_items,
            CAPTION_BATCH_SIZE,
            checkpoint["categories"],
            checkpoint,
            checkpoint_path,
            foundry,
        )

        if set(checkpoint["comment_labels"]) != {
            item["id"] for item in comment_items
        }:
            raise RuntimeError("Comment classifications are incomplete.")
        if set(checkpoint["categories"]) != {
            item["id"] for item in caption_items
        }:
            raise RuntimeError("Publication categories are incomplete.")

        operations = []
        expected_updates: list[tuple[Any, dict[str, Any], dict[str, list[str]]]] = []
        sentiment_counts: dict[str, int] = {}
        tone_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        updated_comment_count = 0
        updated_category_count = 0

        for record in update_records:
            set_values: dict[str, Any] = {}
            expected_fields: dict[str, Any] = {}
            original_comments: dict[str, list[str]] = {}
            for network in PLATFORMS:
                platform = record["platforms"][network]
                if platform["needs_comments"]:
                    labels = []
                    for index, item_id in enumerate(platform["comment_refs"]):
                        if item_id is None:
                            result = {"sentimiento": "neutro", "tono": []}
                        else:
                            result = checkpoint["comment_labels"][item_id]
                        labels.append(
                            {
                                "index": index,
                                "sentimiento": result["sentimiento"],
                                "tono": result["tono"],
                            }
                        )
                        sentiment = result["sentimiento"]
                        sentiment_counts[sentiment] = (
                            sentiment_counts.get(sentiment, 0) + 1
                        )
                        for tone in result["tono"]:
                            tone_counts[tone] = tone_counts.get(tone, 0) + 1
                    path = f"{network}.comment_labels"
                    set_values[path] = labels
                    expected_fields[path] = labels
                    original_comments[network] = platform["comments"]
                    updated_comment_count += len(labels)

            if record["needs_category"]:
                category = checkpoint["categories"][record["caption_id"]]
                set_values["rubro"] = category
                expected_fields["rubro"] = category
                category_counts[category] = category_counts.get(category, 0) + 1
                updated_category_count += 1

            if set_values:
                operations.append(
                    UpdateOne(update_preconditions(record), {"$set": set_values})
                )
                expected_updates.append(
                    (record["_id"], expected_fields, original_comments)
                )

        if not operations:
            raise RuntimeError("No MongoDB updates were created for pending work.")
        write_result = collection.bulk_write(operations, ordered=True)
        if write_result.matched_count != len(operations):
            raise RuntimeError(
                f"MongoDB matched {write_result.matched_count} of "
                f"{len(operations)} documents."
            )

        for document_id, expected_fields, original_comments in expected_updates:
            current = collection.find_one(
                {"_id": document_id},
                {
                    "rubro": 1,
                    "facebook.comment_labels": 1,
                    "instagram.comment_labels": 1,
                },
            )
            if current is None:
                raise RuntimeError(
                    f"Document {stable_id(document_id)} disappeared after update."
                )
            for path, expected in expected_fields.items():
                if path == "rubro":
                    if current.get("rubro") != expected:
                        raise RuntimeError("MongoDB verification failed for rubro.")
                    continue
                network, field = path.split(".", 1)
                actual = (current.get(network) or {}).get(field)
                if actual != expected:
                    raise RuntimeError(
                        f"MongoDB verification failed for {network}.{field}."
                    )
                if field == "comment_labels" and not is_valid_comment_labels(
                    original_comments[network], actual
                ):
                    raise RuntimeError(
                        f"MongoDB comment labels are invalid for {network}."
                    )

        print(
            f"Updated and verified {write_result.matched_count} documents; "
            f"{updated_comment_count} comment labels; "
            f"{updated_category_count} publication categories."
        )
        print(
            "Sentiments="
            + json.dumps(sentiment_counts, ensure_ascii=False, sort_keys=True)
        )
        print(
            "Tones=" + json.dumps(tone_counts, ensure_ascii=False, sort_keys=True)
        )
        print(
            "Categories="
            + json.dumps(category_counts, ensure_ascii=False, sort_keys=True)
        )

        for path in (checkpoint_path, checkpoint_path.with_suffix(
            checkpoint_path.suffix + ".tmp"
        )):
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                print(
                    f"Warning: could not remove checkpoint {path} "
                    f"({type(error).__name__}).",
                    file=sys.stderr,
                )
        return 0
    finally:
        mongo_client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Label social media comments and publication categories."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect pending work without calling Foundry or writing to MongoDB.",
    )
    parser.add_argument(
        "--relabel",
        action="store_true",
        help="Reclassify all comments and descriptions, including existing labels.",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="Checkpoint file path used to resume an interrupted labeling run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_pipeline(args)
    except PyMongoError as error:
        print(
            f"MongoDB operation failed ({type(error).__name__}). Check the URI, "
            "network access, and database permissions.",
            file=sys.stderr,
        )
        return 1
    except (RuntimeError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted. The checkpoint was kept so the run can be resumed.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
