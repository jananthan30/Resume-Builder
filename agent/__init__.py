"""Public, cloud-hostable agent package for the Resume Team runtime.

Everything under ``agent/`` must import cleanly with no ``cloud/`` package
present and with no vendor SDK or API key configured -- see
``agent/host_anthropic.py`` for the Anthropic API host adapter.
"""
