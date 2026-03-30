"""Smart Turn — ML + heuristic turn detection.

Uses DistilBERT sentence completion classifier + text heuristics
to determine if user has finished speaking. ~50ms on CPU.
"""

import logging
import re

logger = logging.getLogger("smart-turn")

# Pre-load classifier at import time (not lazy)
_classifier = None

def _get_classifier():
    global _classifier
    if _classifier is not None:
        return _classifier
    try:
        from transformers import pipeline as hf_pipeline
        logger.info("Loading Smart Turn classifier (DistilBERT)...")
        _classifier = hf_pipeline(
            "text-classification",
            model="KoljaB/SentenceFinishedClassification",
            device=-1,  # CPU
        )
        logger.info("Smart Turn classifier loaded")
    except Exception as e:
        logger.error("Failed to load Smart Turn classifier: %s", e)
    return _classifier

# Pre-load on import — blocks startup for ~10s but prevents mid-call delay
try:
    _get_classifier()
except Exception:
    pass


# Words that clearly indicate user is mid-sentence
MID_SENTENCE_WORDS = {
    "like", "and", "but", "so", "or", "because", "basically",
    "the", "a", "my", "our", "i", "we", "they", "this", "that",
    "um", "uh", "well", "actually", "honestly", "see",
    "about", "how", "what", "why", "when", "where", "which",
    "if", "then", "also", "just", "maybe", "probably",
}

# Phrases that ALWAYS mean "more is coming" — never treat as complete
MID_SENTENCE_PHRASES = [
    "tell me", "let me", "show me", "give me", "send me",
    "what about", "how about", "what if", "so basically",
    "okay so", "right so", "yeah so", "and then", "but what",
    "i want", "i need", "i think", "i mean", "can you",
    "do you", "would you", "could you", "is there", "are there",
    "i said", "no i", "no no", "wait i", "hold on",
    "for you", "for you to", "what i", "what i meant",
]

# Short words that are clearly complete answers
COMPLETE_SHORT = {
    "yeah", "yes", "no", "nope", "okay", "ok", "sure", "right",
    "yep", "yup", "nah", "fine", "cool", "great", "thanks",
    "bye", "hello", "hi", "hey",
}


def predict_turn_complete(text: str = "", audio_pcm_16k=None) -> dict:
    """Predict whether user has finished speaking.

    Combines ML classifier + text heuristics for best accuracy.

    Returns:
        {"complete": bool, "probability": float}
    """
    if not text or not text.strip():
        return {"complete": False, "probability": 0.0}

    text = text.strip()
    words = text.lower().rstrip(".,!?").split()
    word_count = len(words)
    last_word = words[-1] if words else ""

    # ── Heuristic layer (fast, handles edge cases ML misses) ──
    text_lower = text.lower()

    # Check for mid-sentence PHRASES first (highest priority)
    for phrase in MID_SENTENCE_PHRASES:
        if text_lower.rstrip(".,!?").endswith(phrase) or text_lower.rstrip(".,!?").endswith(phrase + "."):
            return {"complete": False, "probability": 0.05}

    # Very short complete answers (1 word)
    if word_count <= 2 and last_word in COMPLETE_SHORT:
        return {"complete": True, "probability": 0.9}

    # Ends with comma or mid-sentence word → definitely not done
    if text.rstrip().endswith(",") or last_word in MID_SENTENCE_WORDS:
        return {"complete": False, "probability": 0.1}

    # Ends with ? or ! → definitely done
    if text.rstrip().endswith("?") or text.rstrip().endswith("!"):
        return {"complete": True, "probability": 0.95}

    # ── ML classifier layer (handles ambiguous cases) ──
    classifier = _get_classifier()
    if classifier is None:
        # No ML — use heuristic only
        # Sentences ending with period are likely complete
        if text.rstrip().endswith("."):
            return {"complete": True, "probability": 0.8}
        # Longer sentences (5+ words) without continuation words are likely complete
        if word_count >= 5:
            return {"complete": True, "probability": 0.7}
        return {"complete": True, "probability": 0.5}

    try:
        result = classifier(text)[0]
        label = result["label"]
        score = result["score"]

        # LABEL_1 = complete, LABEL_0 = incomplete
        if label == "LABEL_1":
            ml_prob = score
        else:
            ml_prob = 1.0 - score

        # Blend ML with heuristic (70% ML, 30% heuristic)
        heuristic_prob = 0.8 if text.rstrip().endswith(".") else (0.6 if word_count >= 4 else 0.4)
        final_prob = 0.7 * ml_prob + 0.3 * heuristic_prob

        return {"complete": final_prob > 0.5, "probability": round(final_prob, 3)}

    except Exception as e:
        logger.warning("ML classifier failed: %s", e)
        return {"complete": True, "probability": 0.5}


def get_dynamic_delay(probability: float) -> float:
    """Convert completion probability to wait delay.

    High probability (done speaking) → short delay (0.3s)
    Low probability (still talking) → longer delay (1.5s)

    Optimized for speed — rely on Smart Turn accuracy.
    """
    if probability > 0.9:
        return 0.3  # Very confident they're done — respond fast
    elif probability > 0.7:
        return 0.5  # Probably done
    elif probability > 0.5:
        return 0.8  # Unclear — wait a bit
    else:
        return 1.2  # Probably not done — wait for more
