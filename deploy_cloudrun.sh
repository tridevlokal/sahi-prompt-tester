#!/bin/bash
# Deploy the voice tester to Google Cloud Run (Mumbai).
# Reads API keys from .env + gcp-service-account.json and passes them as env vars.
# Usage: ./deploy_cloudrun.sh
set -euo pipefail
cd "$(dirname "$0")"
GCLOUD=/opt/homebrew/share/google-cloud-sdk/bin/gcloud
PROJECT=cogent-range-460407-a2
REGION=asia-south1
SERVICE=ai-riya-voice-tester

ENV_FILE=$(mktemp -t riya-env).yaml
trap 'rm -f "$ENV_FILE"' EXIT
venv/bin/python - "$ENV_FILE" <<'PY'
import sys, yaml
from dotenv import dotenv_values
env = dotenv_values(".env")
keys = ["GOOGLE_API_KEY","ELEVENLABS_API_KEY","ELEVENLABS_TTS_API_KEY","SARVAM_API_KEY",
        "SMALLEST_API_KEY","DEEPGRAM_API_KEY","GROQ_API_KEY","ELEVENLABS_BASE_URL",
        "VOICE_ID_ENGLISH","VOICE_ID_TAMIL","VOICE_ID_TELUGU","VOICE_ID_KANNADA"]
out = {k: env[k] for k in keys if env.get(k)}
out["GCP_SERVICE_ACCOUNT_JSON"] = open("gcp-service-account.json").read()
open(sys.argv[1], "w").write(yaml.safe_dump(out))
print("env vars:", ", ".join(out))
PY

$GCLOUD run deploy "$SERVICE" \
  --project "$PROJECT" --region "$REGION" \
  --source . \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 --cpu-boost \
  --min-instances 0 --max-instances 3 \
  --timeout 3600 --session-affinity \
  --env-vars-file "$ENV_FILE"

echo
echo "URL:"
$GCLOUD run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)'
