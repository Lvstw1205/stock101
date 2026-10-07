"""Worker-only local model compatibility; upstream package stays unmodified."""
def configure_local_model(config):
    if config['provider']!='ollama':return
    from tradingagents.llm_clients.openai_client import OpenAIClient
    original=OpenAIClient.get_llm
    def bounded(client):
        if client.provider=='ollama':
            client.kwargs['timeout']=180
            client.kwargs['max_retries']=0
        llm=original(client)
        if client.provider=='ollama':
            # Ollama's OpenAI endpoint uses reasoning_effort=none. Upstream's
            # model-name filter only forwards it for OpenAI model names.
            llm.reasoning_effort='none'
            llm.extra_body={**(llm.extra_body or {}),'max_tokens':config['max_tokens']}
        return llm
    OpenAIClient.get_llm=bounded
