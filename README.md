# Azure OpenAI and On-prem (Whisperv3) ASR

## For COG containers

1. Install COG from Replicate by following the instructions here: https://github.com/replicate/cog
2. For on-prem transcription, you will need:
    1. Nvidia P100 or above 16GB Vram GPU
    2. Docker + Docker-compose or Podman + Podman-Compose
    3. Nvidia drivers and CUDA v11.8 for the onprem host
    4. Go into the folder and run `cog build -t transcriber-api`
    5. Use Docker/Podman to run the container or write a compose to deploy it
3. For Azure OpenAI Whisper API you can deploy as above without worrying about hardware.


# The API expects the following optional inputs in JSON:
1. Audio
2. Language (optional)

# You will get the following in JSON:
{"text":transcript}
    

