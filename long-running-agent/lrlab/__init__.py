"""lrlab: a long-running agent in plain Python, one module per box of the architecture.

    channels / triggers  ->  gateway  ->  per-conversation queue  ->  worker + harness
                                                                        |      |
                                                     model adapters <---+      +---> runner (agent's computer)
                                                                        |
                                                              state store (SQLite) ---> deliveries / live SSE
"""
