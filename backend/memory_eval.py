import tempfile
from pathlib import Path
from typing import Any

import database
import memory
from database import initialize_database

MEMORIES = [
    ('grandmother-windmill', 'episode', '子どもの頃、祖母と青い風車を見に行った。'),
    ('grandfather-onsen', 'episode', '子どもの頃、祖父と長野の温泉に出かけた。'),
    ('sister-guitar', 'episode', '姉からギターの弾き方を教わった。'),
    ('osaka-university', 'episode', '大学進学を機に大阪へ引っ越した。'),
    ('coffee-preference', 'preference', 'コーヒーが好きで、特に浅煎りをよく飲む。'),
    ('dog-mugi', 'episode', '保護施設から迎えた犬の名前はムギ。'),
    ('quiet-mornings', 'preference', '朝は静かに一人で過ごすのが好き。'),
    ('summer-festival-friend', 'episode', '夏祭りで昔の友人ユウに再会した。'),
    ('mother-apple-tart', 'episode', '母と一緒にりんごのタルトを焼いた。'),
    ('okinawa-first-trip', 'episode', '恋人と初めて旅行した場所は沖縄。'),
    ('work-life-balance', 'preference', '仕事では生活と仕事のバランスを大切にしたい。'),
    ('cycling-hobby', 'preference', '休日の趣味は自転車で川沿いを走ること。'),
]

CASES = [
    ('positive', 'おばあちゃんと昔見に行った青いものは何？', 'grandmother-windmill', [
        '祖母と青い風車を見に行った思い出',
    ]),
    ('positive', 'じいちゃんと旅行した場所はどこだっけ？', 'grandfather-onsen', [
        '祖父と長野の温泉に出かけた思い出',
    ]),
    ('positive', 'お姉ちゃんに習った楽器って何？', 'sister-guitar', []),
    ('positive', '進学で関西のどこへ移ったんだっけ？', 'osaka-university', [
        '大学進学を機に大阪へ引っ越した',
    ]),
    ('positive', '前に好きだと言っていた飲み物は？', 'coffee-preference', [
        '浅煎りコーヒーが好きだと言っていた',
    ]),
    ('positive', '保護施設から迎えた犬の名前なんだっけ？', 'dog-mugi', []),
    ('positive', '朝はどうやって過ごすのが好き？', 'quiet-mornings', []),
    ('positive', '夏祭りで再会した人は誰？', 'summer-festival-friend', []),
    ('positive', 'お母さんと一緒に焼いたお菓子は何？', 'mother-apple-tart', [
        '母親とりんごのタルトを焼いた思い出',
    ]),
    ('positive', '恋人との初旅行はどの島だった？', 'okinawa-first-trip', []),
    ('positive', '働くうえで大事にしている考え方は？', 'work-life-balance', []),
    ('positive', '休みの日に川沿いでする趣味は？', 'cycling-hobby', []),
    ('negative', '祖父と昔見に行った青いものは何？', None, []),
    ('negative', '祖母と長野で入った温泉はどこ？', None, []),
    ('negative', '兄から習った楽器は何？', None, []),
    ('negative', '京都の大学へ進んだことを話した？', None, []),
    ('negative', '紅茶のどの銘柄が好き？', None, []),
    ('negative', '飼っている猫の名前は？', None, []),
    ('negative', '夜は一人で何をするのが好き？', None, []),
    ('negative', '花火大会で再会した先輩は誰？', None, []),
    ('negative', '父親と作ったラーメンの思い出は？', None, []),
    ('negative', '家族で初めて行った北海道旅行はいつ？', None, []),
    ('negative', '仕事で今困っている具体的な問題は？', None, []),
    ('negative', '平日の通勤で使う交通手段は？', None, []),
    ('negative', '初めて飼ったハムスターの名前は？', None, []),
    ('negative', '来週の会議は何時？', None, []),
    ('negative', '今日は何を食べたらいいと思う？', None, []),
    ('negative', '好きな映画のタイトルは？', None, []),
]
THRESHOLDS = (0.66, 0.68, 0.70, 0.71, 0.72, 0.74, 0.76)


def evaluate_threshold(threshold: float, memory_ids: dict[str, str]) -> dict[str, Any]:
    memory.MIN_MEMORY_SIMILARITY = threshold
    correct = 0
    missed = 0
    wrong_positive = 0
    false_recall = 0
    false_recall_queries = []
    missed_queries = []

    for case_type, query, expected_key, variants in CASES:
        category = next(category for key, category, _ in MEMORIES if key == expected_key) if expected_key else None
        results = memory.search_memory_records(
            query,
            category=category,
            limit=3,
            query_variants=variants,
        )
        top_id = results[0]['id'] if results else None
        expected_id = memory_ids.get(expected_key) if expected_key else None
        if case_type == 'positive':
            if top_id == expected_id:
                correct += 1
            elif top_id is None:
                missed += 1
                missed_queries.append(query)
            else:
                wrong_positive += 1
        elif results:
            false_recall += 1
            false_recall_queries.append(query)

    total_positive = sum(case[0] == 'positive' for case in CASES)
    total_negative = sum(case[0] == 'negative' for case in CASES)
    denominator = correct + wrong_positive + false_recall
    precision = correct / denominator if denominator else 0.0
    recall = correct / total_positive
    return {
        'threshold': threshold,
        'correct': correct,
        'missed': missed,
        'wrong_positive': wrong_positive,
        'false_recall': false_recall,
        'false_recall_queries': false_recall_queries,
        'missed_queries': missed_queries,
        'positive_total': total_positive,
        'negative_total': total_negative,
        'precision': precision,
        'recall': recall,
    }


def evaluate() -> list[dict[str, Any]]:
    old_database_path = database.DB_PATH
    old_threshold = memory.MIN_MEMORY_SIMILARITY
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix='norn-memory-eval-') as temp_dir:
            database.DB_PATH = Path(temp_dir) / 'evaluation.sqlite3'
            initialize_database()
            memory_ids = {
                key: memory.save_memory_record(category, content, 'explicit')['id']
                for key, category, content in MEMORIES
            }
            for threshold in THRESHOLDS:
                results.append(evaluate_threshold(threshold, memory_ids))
    finally:
        database.DB_PATH = old_database_path
        memory.MIN_MEMORY_SIMILARITY = old_threshold
    return results


def main() -> None:
    for result in evaluate():
        print(
            'threshold={threshold:.2f} '
            'correct={correct}/{positive_total} '
            'missed={missed} wrong_positive={wrong_positive} '
            'false_recall={false_recall}/{negative_total} '
            'precision={precision:.2f} recall={recall:.2f}'.format(**result)
        )
        if result['threshold'] == 0.72 and result['false_recall_queries']:
            print('remaining false recalls: ' + ' | '.join(result['false_recall_queries']))
        if result['threshold'] == 0.72 and result['missed_queries']:
            print('missed positives: ' + ' | '.join(result['missed_queries']))


if __name__ == '__main__':
    main()
