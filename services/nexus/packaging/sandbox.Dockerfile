# Sandbox image for NEXUS working on its own repository: the service's dependencies and the test runner are
# preinstalled, because sandboxed runs have no network. The code under test is NOT baked in: the task worktree is
# mounted read-only at /work, and PYTHONPATH makes the worktree's copy of the package the one that is imported.
#
# Build (context = services/nexus):
#   docker build -f packaging/sandbox.Dockerfile -t nexus-sandbox:py312 services/nexus
# Then, in NEXUS: Connected services -> Sandbox -> image "nexus-sandbox:py312".
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /tmp/pyproject.toml
RUN python -c "import tomllib; d = tomllib.load(open('/tmp/pyproject.toml', 'rb'))['project']; \
print('\n'.join(d['dependencies'] + ['pytest>=8', 'pytest-asyncio>=0.24']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt /tmp/pyproject.toml

# Read-only worktree, writable /tmp only: keep caches, HOME and git identity in /tmp; trust the mounted repo.
ENV PYTHONPATH=/work/services/nexus \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    NEXUS_SECRET_BACKEND=memory \
    GIT_AUTHOR_NAME=nexus-sandbox GIT_AUTHOR_EMAIL=sandbox@nexus.invalid \
    GIT_COMMITTER_NAME=nexus-sandbox GIT_COMMITTER_EMAIL=sandbox@nexus.invalid \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*

# Default test command for this repository (pytest cache disabled: the worktree is read-only).
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/service"]
