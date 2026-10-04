from sieve.chunking import chunk_text


def test_strategy_interface_is_private_to_registry_implementation():
    import sieve.chunking as chunking

    assert not hasattr(chunking, "ChunkingStrategy")


def test_chunk_text_strategies():
    assert chunk_text("hello", "identity") == ["hello"]
    assert chunk_text("a\n\nb", "regex") == ["a", "b"]


def test_chunk_text_unknown_strategy():
    try:
        chunk_text("x", "bogus")
    except ValueError as e:
        assert "bogus" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_sentence_chunking_without_nltk():
    import sieve.chunking as c
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "nltk.tokenize":
            raise ImportError("No module named 'nltk'")
        return real_import(name, *args, **kwargs)

    try:
        builtins.__import__ = fake_import
        try:
            c.NltkSentenceChunking().chunk("Hello world. Bye.")
        except RuntimeError as e:
            assert "uv sync --extra nlp" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        builtins.__import__ = real_import
