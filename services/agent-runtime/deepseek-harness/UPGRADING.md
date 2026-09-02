# Upgrading DeepSeek Harness

1. Read the upstream release notes and safety notice.
2. Change only the exact `deepseek-harness-sdk` pin in `pyproject.toml` and `HARNESS_SDK_VERSION` in `runtime/__init__.py`.
3. Build a new Agent Runtime image without changing the core backend or web app.
4. Run v1/v2 contract, model-call, Skills hash/loading, project-file diff, tool-policy, session-resume and tenant-isolation tests.
5. Deploy the new image beside the previous version and route test tenants to it.
6. Verify execution manifests and historical-session compatibility before increasing traffic.
7. Keep the previous image and its compatible Harness home volumes available for rollback.

Never replace the version pin with `latest`, copy upstream source into this directory, or patch upstream internals inline. Compatibility patches belong in a dedicated `patches/` directory and must document the exact upstream version and reason.
