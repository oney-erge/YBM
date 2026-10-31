"""The models and llama-server settings the benchmark compares (RTX 3080 Laptop 8 GB, 40 GB RAM).

Every entry runs at the same 16K context YBM uses today. Unless an entry says otherwise it leaves layer placement
to llama-server's --fit, which fills the GPU and puts the rest (for mixture-of-experts models, the expert weights)
in RAM. "ncmoe" entries pin how many layers keep their experts in RAM, to compare against that automatic choice.
"""

from __future__ import annotations

from pathlib import Path

from llama import ServerConfig

# Flash attention and an 8-bit KV cache roughly halve the cache's VRAM; thinking is off because the Operator
# answers in one JSON object and a reasoning prefix only adds latency here.
COMMON = ["-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--reasoning", "off", "-np", "1", "-fitt", "1024"]

FILES = {
    # Today's model, from the standard GGUF (Ollama keeps its own variant that llama.cpp cannot load).
    "qwen3-vl-8b": "qwen3-vl-8b/Qwen3-VL-8B-Instruct-Q4_K_M.gguf",
    "qwen3.5-4b": "qwen3.5-4b/Qwen3.5-4B-Q4_K_M.gguf",
    "qwen3.5-9b": "qwen3.5-9b/Qwen3.5-9B-Q4_K_M.gguf",
    "gemma-4-e4b": "gemma-4-e4b/gemma-4-E4B-it-Q4_K_M.gguf",
    "glm-4.6v-flash": "glm-4.6v-flash/GLM-4.6V-Flash-Q4_K_M.gguf",
    "gemma-4-12b": "gemma-4-12b/gemma-4-12b-it-Q4_K_M.gguf",
    "gpt-oss-20b": "gpt-oss-20b/gpt-oss-20b-Q4_K_M.gguf",
    "gemma-4-26b-a4b": "gemma-4-26b-a4b/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
    "qwen3.6-35b-a3b": "qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
    "qwen3.6-27b": "qwen3.6-27b/Qwen3.6-27B-Q4_K_S.gguf",
}
# Placements to try for the mixture-of-experts models (layers whose experts stay in RAM).
NCMOE = {"gpt-oss-20b": [20, 12], "gemma-4-26b-a4b": [99, 24], "qwen3.6-35b-a3b": [99, 30]}


def build(models_dir: Path) -> dict[str, ServerConfig]:
    configs: dict[str, ServerConfig] = {}
    for name, relative in FILES.items():
        path = models_dir / relative
        if path.is_file():
            configs[name] = ServerConfig(name, str(path), COMMON)
            if name == "qwen3.5-9b":
                # Thinking on, capped, to see whether reasoning before the JSON buys accuracy worth its seconds.
                thinking = [arg for arg in COMMON]
                index = thinking.index("--reasoning")
                thinking[index + 1] = "on"
                configs["qwen3.5-9b (thinking, 768-token budget)"] = ServerConfig(
                    "qwen3.5-9b-thinking", str(path), [*thinking, "--reasoning-budget", "768"]
                )
            for count in NCMOE.get(name, []):
                variant = f"{name} (experts in RAM: {count} layers)"
                configs[variant] = ServerConfig(variant, str(path), [*COMMON, "--n-cpu-moe", str(count)])
    return configs
