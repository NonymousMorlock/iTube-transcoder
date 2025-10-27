iTube-transcoder
=================

Overview
--------

iTube-transcoder is a small service that downloads a video from S3, transcodes it (HLS/DASH using FFmpeg), and uploads processed outputs back to an S3 bucket. Logging is centralized in `app/core/logging_config.py`. The entrypoint is `app/main.py`.

Prerequisites
-------------

- Python 3.13
- ffmpeg on PATH (the Dockerfile installs ffmpeg inside the image)
- AWS credentials with access to the S3 buckets used (GetObject on input, PutObject on processed outputs)
- Docker (if building/running the image)

Environment variables
---------------------

Copy and edit the provided example file `.env.example` to create a local `.env` that contains the required environment variables. The required variables are:

- REGION_NAME
- AWS_ACCESS_KEY_ID
- AWS_SECRET_ACCESS_KEY
- S3_PROCESSED_VIDEOS_BUCKET
- S3_BUCKET
- S3_KEY

Note: `S3_BUCKET` and `S3_KEY` are often provided by the consumer service that triggers this transcoder (for example, via environment injection or a message payload). In many deployments you won't need to hard-code these into `.env`; the calling service or orchestrator injects them at runtime.

Do NOT commit `.env` to source control — treat it like your embarrassing search history: private, personal, and best kept off the internet. (Also: add `.env` to `.gitignore`.)

Local development (quick start)
-------------------------------

1. Create & activate a virtualenv and install deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create a `.env` from the example and fill in your values:

```bash
cp .env.example .env
# Edit .env with your credentials and S3 keys
```

3. Export the env vars and run the app:

```bash
export $(grep -v '^#' .env | xargs)
python3 -m app.main
```

Notes
- The application validates required environment variables via Pydantic settings at import time. If you see validation errors on import, ensure the required env vars are set.
- If you want to perform a quick import-only check without valid AWS credentials, set placeholder values for the required env vars to avoid validation errors (boto3 will still attempt to use credentials when you call S3 APIs).

Docker: build and run locally
----------------------------

The repository includes a `Dockerfile` that installs `ffmpeg` and your Python dependencies. Example build and run commands:

```bash
# Build the local image
docker build -t itube-transcoder .

# Run with environment variables loaded from .env
docker run --rm --env-file .env itube-transcoder
```

Pushing the image to Amazon ECR (IMPORTANT)
-------------------------------------------

This repository's images should be pushed to your project's ECR repository. The exact commands to authenticate and push an image depend on the ECR repository URI and region. The AWS ECR Console provides an exact, step-by-step set of commands for your repository labeled "View push commands" — use those commands to push the image.

A general example (replace placeholders with your values):

```bash
# Tag the local image with your ECR repo URI
docker tag itube-transcoder:latest <aws_account_id>.dkr.ecr.<region>.amazonaws.com/<repo_name>:latest

# Authenticate Docker to ECR (example using AWS CLI)
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <aws_account_id>.dkr.ecr.<region>.amazonaws.com

# Push the image
docker push <aws_account_id>.dkr.ecr.<region>.amazonaws.com/<repo_name>:latest
```

To ensure you run the exact sequence required for this specific project/repository, open the ECR Console, navigate to the repository for this project, and click "View push commands" (this shows the appropriate `aws ecr` login command plus the exact tag to use). Use those commands rather than the generic example above.

Logging and error handling
--------------------------

- Logging is configured centrally in `app/core/logging_config.py`. The `main` script calls `configure_logging()` at startup.
- Service-level code (in `app/services/transcoder.py`) raises a domain-specific `TranscodingError` when ffmpeg fails; the error carries ffmpeg return code, stdout and stderr. `main` inspects this exception and logs truncated/stored output for diagnostics, then exits with a non-zero code.
- Exit codes used by `main.py`:
  - 0: Success
  - 1: Unexpected runtime error (S3, filesystem, etc.)
  - 2: Initialization error (missing/invalid settings or AWS client init error)
  - 3: Transcoding error (ffmpeg failure)

Troubleshooting
---------------

- ffmpeg not found: install ffmpeg locally, or run inside the Docker image (the provided Dockerfile installs it).
- AWS permission errors: ensure the provided credentials allow s3:GetObject for the input and s3:PutObject for the output bucket/prefix.
- Large ffmpeg output: the service captures stdout/stderr and `TranscodingError` contains it; `main` logs truncated snippets for easier debugging.

Further improvements
--------------------

- Add a "local mode" to allow passing a local input file path for development without S3.
- Add unit/integration tests around `_command_builder()` and a small integration test that runs ffmpeg on a tiny test clip.
- Switch logging to structured JSON if you plan to ship logs to a logging/observability backend.

License
-------

This project is available under the license in the [LICENSE](LICENSE) file.

Contact / Support
-----------------

If you’re stuck on pushing to ECR (we’ve *all* been there, mate), check out this [step-by-step AWS troubleshooting guide](https://lmgt.org/?q=aws+ecr+push+docker+image+step+by+step).

Didn’t solve it? Try searching the official AWS docs for “**ECR push commands**” or “**pushing a Docker image to Amazon ECR**.” Stack Overflow’s also full of fine folks who’ve probably broken the same thing you just did.

