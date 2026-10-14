from app.asr import SR, Word, merge_speech
from app.exporters import FORMATS, clock, to_srt, ts
from app.diarize import cleanup
from app.textlayout import CUE_LINE, CUE_MAX_CHARS, CUE_MAX_S, cues, layout, paragraphs, retime, sentences, speaker_names


def words_from(text: str, start: float = 0.0, step: float = 0.4, gap_after: dict | None = None) -> list[Word]:
    out, t = [], start
    for i, tok in enumerate(text.split()):
        out.append(Word(tok, round(t, 3), round(t + step * 0.8, 3)))
        t += step + (gap_after or {}).get(i, 0.0)
    return out


def test_sentences_split_on_punctuation():
    ws = words_from("Привет. Меня зовут Ира, и это мой канал! Правда? Да…")
    sents = sentences(ws)
    assert [s.text for s in sents] == ["Привет.", "Меня зовут Ира, и это мой канал!", "Правда?", "Да…"]
    assert sents[1].start == ws[1].start and sents[1].end == ws[7].end


def test_long_pause_starts_new_sentence_and_paragraph():
    ws = words_from("один два Три четыре пять", gap_after={1: 3.0})
    sents = sentences(ws)
    assert [s.text for s in sents] == ["один два", "Три четыре пять"]
    assert len(paragraphs(sents)) == 2


def test_pause_inside_sentence_does_not_split():
    ws = words_from("Полтора, наверное, у меня тоже. Сейчас", gap_after={1: 3.0})
    assert [s.text for s in sentences(ws)] == ["Полтора, наверное, у меня тоже.", "Сейчас"]


def test_speaker_change_splits_sentences_and_paragraphs():
    ws = words_from("Как дела у тебя сегодня Отлично спасибо а у тебя")
    for i, w in enumerate(ws):
        w.speaker = 0 if i < 5 else 1
    ws[2].speaker = 1  # ensamt brusord mitt i talare 0 ska jämnas ut
    doc = layout(ws)
    paras = doc["paragraphs"]
    assert [p["speaker"] for p in paras] == [0, 1]
    assert paras[0]["sentences"][0]["text"] == "Как дела у тебя сегодня"
    assert speaker_names(doc) == {"0": "Talare 1", "1": "Talare 2"}
    assert speaker_names(doc, {"1": "Даша"})["1"] == "Даша"


def test_cleanup_merges_tiny_speaker_clusters():
    segs = [(0, 30, 7), (30, 31, 3), (31, 60, 2), (60, 61, 9), (61, 90, 7)]
    out = cleanup(segs)
    assert {s for _, _, s in out} == {0, 1}
    assert out[0] == (0, 31, 0)  # småklustret 3 hamnar hos närmaste talare


def test_cues_respect_limits_and_never_overlap():
    text = " ".join(["слово,"] * 12 + ["конец."] + ["очень длинное предложение без точки"] * 10)
    ws = words_from(text, step=0.35)
    # Grannchunkar kan ge omlott-tider: simulera det.
    ws[20] = Word(ws[20].text, ws[19].start, ws[20].end)
    cs = cues(sentences(ws))
    assert cs
    for c in cs:
        assert len(c.text.replace("\n", " ")) <= CUE_MAX_CHARS
        assert all(len(line) <= CUE_LINE for line in c.text.split("\n")), c.text
        assert c.end - c.start <= CUE_MAX_S + 1e-6
        assert c.end > c.start
    for a, b in zip(cs, cs[1:]):
        assert a.end <= b.start + 1e-9


def test_layout_roundtrip_has_all_words():
    ws = words_from("Это первый. Это второй. А это третий абзац.", gap_after={3: 4.0})
    doc = layout(ws)
    flat = [w[0] for p in doc["paragraphs"] for s in p["sentences"] for w in s["words"]]
    assert flat == [w.text for w in ws]
    assert len(doc["paragraphs"]) == 2


def test_merge_speech_limits_and_gaps():
    sec = lambda x: int(x * SR)  # noqa: E731
    stamps = [
        {"start": sec(0.0), "end": sec(5.0)},
        {"start": sec(5.5), "end": sec(12.0)},
        {"start": sec(12.3), "end": sec(21.9)},  # 21.9 s + padding > 22 s → ny chunk
        {"start": sec(30.0), "end": sec(31.0)},  # lång paus → ny chunk
    ]
    chunks = merge_speech(stamps, sec(40))
    assert len(chunks) == 3
    for c in chunks:
        assert (c.end - c.start) / SR <= 22.0
    for a, b in zip(chunks, chunks[1:]):
        assert a.end <= b.start


def test_timestamps_format():
    assert ts(3725.5) == "01:02:05,500"
    assert ts(0.0469, ".") == "00:00:00.047"
    assert clock(59) == "0:59" and clock(3600) == "1:00:00"


def test_all_exporters_render():
    ws = words_from("Привет. Меня зовут Ира, и это мой канал о русском по-русски.")
    doc = {"meta": {"title": "Тест", "url": "https://example.com"}, "transcript": layout(ws)}
    for fmt, (_, fn) in FORMATS.items():
        out = fn(doc)
        assert out, fmt
    srt = to_srt(doc)
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert "Меня зовут Ира" in srt


def test_retime_keeps_times_when_word_count_matches():
    sent = {"start": 1.0, "end": 3.0, "words": [["Привет", 1.0, 1.5], ["мир.", 2.0, 3.0]]}
    assert retime(sent, "Привет мир!") == [["Привет", 1.0, 1.5], ["мир!", 2.0, 3.0]]
    spread = retime(sent, "Привет большой мир.")
    assert len(spread) == 3 and spread[0][1] == 1.0 and spread[-1][2] == 3.0


def test_tokens_to_words_carries_min_confidence():
    from app.asr import tokens_to_words

    class Tok:
        pieces = {1: "▁при", 2: "вет", 3: "▁мир", 4: "."}

        def id_to_str(self, i):
            return self.pieces[i]

    ws = tokens_to_words(Tok(), [1, 2, 3, 4], [0, 1, 3, 4], [0.9, 0.4, 0.95, 0.8], 0.04)
    assert [w.text for w in ws] == ["привет", "мир."]
    assert ws[0].conf == 0.4 and ws[1].conf == 0.8
    assert ws[0].start == 0.0 and abs(ws[1].end - 0.2) < 1e-9


def test_merge_speakers_by_same_name_rebuilds_paragraphs():
    from app.textlayout import merge_speakers

    ws = words_from("Привет всем здесь. Да это так. Привет еще раз.", step=0.5)
    for w, spk in zip(ws, [0, 0, 0, 1, 1, 1, 2, 2, 2]):
        w.speaker = spk
    doc = layout(ws)
    assert [p["speaker"] for p in doc["paragraphs"]] == [0, 1, 2]
    merged, names = merge_speakers(doc, {"0": "Ира", "1": "Даша", "2": "ира"})
    assert names == {"0": "Ира", "1": "Даша"}
    assert {p["speaker"] for p in merged["paragraphs"]} == {0, 1}
    assert merged["paragraphs"][-1]["speaker"] == 0
