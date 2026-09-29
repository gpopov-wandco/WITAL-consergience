"""Voice fallback (F26): providers, the call session engine, post-call pipeline.

Implements: F26

The same Brain answers calls with a voice profile. ``port`` holds the data types and
the socket abstraction, ``session`` the provider-agnostic engine (disclosure first,
filler after ``filler_ms``, cancellation by a newer response id, transfer and callback
rules), ``retell`` and ``twilio_cr`` the provider protocols and signatures, ``postcall``
the end-of-call pipeline (transcript with expiry, German summary, callback task,
staff notification, usage event) and ``routes`` the HTTP and WebSocket endpoints.
"""
