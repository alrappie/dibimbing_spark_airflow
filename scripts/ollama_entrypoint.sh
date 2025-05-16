#!/bin/bash

# Start the Ollama server in the background
ollama serve &

# Wait for the server to be ready
until curl -s http://localhost:11434 > /dev/null; do
  echo "Waiting for Ollama to start..."
  sleep 1
done

# Download the model (this will pull if not present)
ollama run qwen2.5:7b-instruct

# Keep container running (serve was started in background)
wait
