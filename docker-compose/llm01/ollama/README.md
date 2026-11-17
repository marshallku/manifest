# ollama (llm01)

Local LLM inference on the homelab's RTX 3060 Ti. One base model, many assistants: each assistant
is a system prompt (an Ollama Modelfile, or a preset in a UI) on top of the same weights.

| What | Where |
|---|---|
| Host | `llm01` (pve02 VM 104, 10.20.0.22), infra `tofu/llm01.tf` · `bootstrap/llm01.sh` |
| GPU | RTX 3060 Ti 8GB, passed through (`hostpci` mapping `rtx3060ti`) |
| Models | `/srv/models/ollama` on the `vault` HDD. **Disposable**: not backed up, re-pull instead |
| API | `http://10.20.0.22:11434` — native `/api/*` and OpenAI-compatible `/v1/chat/completions`. **No auth**, LAN only |

## Deploy

```sh
cd ~/stacks/ollama          # a copy of this directory
docker compose up -d
curl -s http://10.20.0.22:11434/api/version
docker exec ollama ollama pull <model>
```

Bumping = change the tag in `docker-compose.yml`. The host driver is Debian's `nvidia-driver`
(550 at creation); a new Ollama release that needs a newer CUDA runtime shows up as the model
running on the CPU — check `docker logs ollama` for the detected GPU after every bump.

## Assistants and private prompts

This repository is public. System prompts that are personal (character sheets, NSFW policy, anything
you would not paste on a forum) do **not** go here: keep their Modelfiles on the host under
`/srv/models/ollama/modelfiles/` — inside the bind mount, so the container sees it as
`/root/.ollama/modelfiles/` — and register them with
`docker exec ollama ollama create <name> -f /root/.ollama/modelfiles/<name>.Modelfile`,
or keep them as presets inside the UI.

## Sizing

8GB of VRAM means one model at a time, up to ~12B parameters at Q4 with an 8k context (flash
attention + q8_0 KV cache, set in the compose file). A cold load reads the model from the HDD; if
that becomes the bottleneck, move the disk online with `qm disk move 104 scsi1 local-zfs`.
