Runtime生成物 config.generated.yaml は4つの実 llama.cpp /v1/models から生成する。
RouterはAI固有Roleを持たず、上位AIはAPI経由で実Model IDを指定する。
Qwen3 thinking等の request option は上位AIから chat_template_kwargs として渡す。
