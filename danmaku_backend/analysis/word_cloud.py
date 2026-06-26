from __future__ import annotations

from collections import Counter

import jieba


STOP_WORDS = {
    "的", "了", "是", "都", "在", "吗", "啊", "这", "有", "和", "就", "我", "你", "他", "她", "它", "们",
    "啦", "呢", "吧", "哈哈", "哈哈哈", "什么", "好", "也", "被", "不", "说", "刚刚", "怎么", "还是",
    "现在", "太", "很", "能", "要", "啥", "去", "会", "到", "这个", "那个", "没有", "自己", "这样",
    "那样", "看看",
}


def analyze_word_frequency(texts: list[str], max_words: int = 150) -> list[dict[str, int | str]]:
    words: list[str] = []
    for text in texts:
        words.extend(jieba.lcut(text or ""))
    word_freq = Counter(word for word in words if len(word) > 1 and word not in STOP_WORDS)
    return [{"text": word, "value": count} for word, count in word_freq.most_common(max_words)]

