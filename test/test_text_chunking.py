import pytest

from tools.text_chunking import text_windows, sentence_boundaries


def assert_complete_windows(text, windows, size, overlap):
    covered = [False] * len(text)
    previous_start, previous_end = -1, 0
    for start, end in windows:
        assert 0 <= start < end <= len(text) and end - start <= size
        assert start > previous_start and end > previous_end
        assert start <= previous_end and previous_end - start <= overlap
        covered[start:end] = [True] * (end - start)
        previous_start, previous_end = start, end
    assert all(covered)


def test_sentence_windows_preserve_sentences_and_reuse_complete_context():
    sentences = [f'Sentence {i:02d} reports a distinct result. ' for i in range(12)]
    text = ''.join(sentences)
    windows = list(text_windows(text, chunk_size=160, chunk_overlap=50))
    assert_complete_windows(text, windows, 160, 50)
    assert any(start < windows[i - 1][1] for i, (start, _) in enumerate(windows) if i)
    cursor = 0
    for sentence in sentences:
        assert any(start <= cursor and cursor + len(sentence.rstrip()) <= end for start, end in windows)
        cursor += len(sentence)


def test_paragraph_break_preferred_when_it_fills_most_of_window():
    first = 'This complete paragraph describes the first experimental result.'
    second = 'Another complete paragraph describes the next experimental result.'
    text = first + '\n\n' + second
    windows = list(text_windows(text, chunk_size=100, chunk_overlap=20))
    assert windows[0] == (0, len(first) + 2)
    assert any(text[start:end] == second for start, end in windows)
    assert_complete_windows(text, windows, 100, 20)


def test_abbreviations_initials_decimals_and_urls_are_not_sentence_ends():
    text = 'Dr. A. Smith reports e.g. 1.25 m accuracy in Fig. 2 and Eq. 3 using https://example.org/data.'
    assert sentence_boundaries(text) == [0, len(text)]


def test_chinese_sentences_and_closing_quotes_stay_complete():
    sentences = [f'第{i}项实验记录了结果。' for i in range(20)] + ['仪器报告“误差为 0.5%。”', '实验结束！']
    text = ''.join(sentences)
    windows = list(text_windows(text, chunk_size=100, chunk_overlap=30))
    assert_complete_windows(text, windows, 100, 30)
    cursor = 0
    for sentence in sentences:
        assert any(start <= cursor and cursor + len(sentence) <= end for start, end in windows)
        cursor += len(sentence)


def test_context_overlap_does_not_force_a_fitting_sentence_to_be_cut():
    sentence = 'c' * 90 + '.'
    text = 'a' * 20 + '. ' + 'b' * 30 + '. ' + sentence
    windows = list(text_windows(text, chunk_size=100, chunk_overlap=50))
    assert any(sentence in text[start:end] for start, end in windows)
    assert_complete_windows(text, windows, 100, 50)


def test_overlong_sentence_uses_word_boundaries_and_stays_bounded():
    text = 'unbroken ' * 80
    windows = list(text_windows(text, chunk_size=100, chunk_overlap=20))
    assert_complete_windows(text, windows, 100, 20)
    assert all(text[end - 1].isspace() for _, end in windows)


@pytest.mark.parametrize('overlap', [0, 20, 99])
def test_no_punctuation_fallback_terminates_without_missing_text(overlap):
    text = '无' * 237
    windows = list(text_windows(text, chunk_size=100, chunk_overlap=overlap))
    assert len(windows) <= len(text)
    assert_complete_windows(text, windows, 100, overlap)


@pytest.mark.parametrize('size,overlap', [(99, 0), (100, -1), (100, 100), (100, 1.5), ('100', 0)])
def test_invalid_window_settings_rejected(size, overlap):
    with pytest.raises(ValueError):
        list(text_windows('source text', chunk_size=size, chunk_overlap=overlap))


def test_empty_and_short_text():
    assert list(text_windows('')) == []
    assert list(text_windows('Short sentence.')) == [(0, 15)]


def test_regression_slam_estimate_sentence_crosses_old_fixed_boundary_but_not_new_windows():
    before = 'Given a sequence of controls and observations, the goal is to estimate'
    after = ' the robot trajectory and the map. '
    prefix_length = 1200 - len(before)
    prefix = 'Earlier result. ' * (prefix_length // len('Earlier result. ') - 1)
    prefix += 'x' * (prefix_length - len(prefix) - 2) + '. '
    text = prefix + before + after
    assert text[1200:].startswith(' the robot trajectory')
    windows = list(text_windows(text))
    assert any(before + after.rstrip() in text[start:end] for start, end in windows)
    assert_complete_windows(text, windows, 1200, 200)
