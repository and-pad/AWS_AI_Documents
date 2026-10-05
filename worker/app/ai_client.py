import os
import logging

import requests

logger = logging.getLogger(__name__)


def send_document(filename: str, content_type: str, content: bytes, user_prompt: str = "") -> dict:
    url = os.environ["DOCUMENT_PROCESSOR_URL"]
    try:
        response = requests.post(
            url,
            files={"file": (filename, content, content_type)},
            data={"user_prompt": user_prompt},
            timeout=(5, 840),
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        logger.exception("document-processor request failed filename=%s", filename)
        raise
