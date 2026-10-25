"""用本機 CLIP 模型判斷一張圖是不是長輩圖（零樣本分類，不用訓練）。"""
import logging

log = logging.getLogger(__name__)

MODEL_NAME = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"

POSITIVE = [
    "a Chinese greeting image with flowers and large colorful text wishing good morning",
    "an elder-style greeting card with blessing words in Chinese calligraphy",
    "a scenic landscape picture with big decorative Chinese text wishing peace and good health",
    "a greeting e-card with Chinese characters, flowers and a cup of tea",
]
NEGATIVE = [
    "a casual photo of people",
    "a selfie",
    "a screenshot of a phone app or chat",
    "a cartoon sticker or emoji",
    "a photo of food",
    "a photo of a pet",
    "a photo of a document or receipt",
    "a meme with a short caption",
]

_model = None
_preprocess = None
_text_features = None


def load():
    """載入模型（第一次會下載約 600MB），之後重複呼叫不會重載。"""
    global _model, _preprocess, _text_features
    if _model is not None:
        return
    import open_clip
    import torch

    log.info("載入 CLIP 模型 %s ...", MODEL_NAME)
    model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=PRETRAINED)
    model.eval()
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)
    with torch.no_grad():
        feats = model.encode_text(tokenizer(POSITIVE + NEGATIVE))
        feats /= feats.norm(dim=-1, keepdim=True)
    _model, _preprocess, _text_features = model, preprocess, feats


def score(img) -> float:
    """回傳 0~1，越高越像長輩圖。"""
    import torch

    load()
    x = _preprocess(img.convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        f = _model.encode_image(x)
        f /= f.norm(dim=-1, keepdim=True)
        probs = (100.0 * f @ _text_features.T).softmax(dim=-1)[0]
    return probs[: len(POSITIVE)].sum().item()


def is_grandma_image(img, threshold=0.6):
    s = score(img)
    return s >= threshold, s
