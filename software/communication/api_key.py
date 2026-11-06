import os

# Shared by the recorder and the services to keep the STM API key out of error messages and logs.
# Does not import communication.protocol (it needs STM_API_KEY at import time) nor communication.stm_api (protobuf).


def redact_key(text:str) -> str:
    """Replaces the STM_API_KEY value in `text`, as is or escaped as in a repr, with <STM_API_KEY>."""
    key = os.environ.get("STM_API_KEY", "")
    if not key.strip():     # nothing secret to hide, and replacing "" would garble the text
        return text
    forms = {form for value in (key, key.strip()) for form in (value, repr(value)[1:-1])}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "<STM_API_KEY>")
    return text
