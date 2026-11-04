import asyncio
from contextvars import ContextVar
import json
import re
import sqlite3
from time import perf_counter
import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_conversation_messages, now_iso, open_database
from events import SCHEDULE_TOOLS, execute_schedule_tool
from memory import MEMORY_TOOLS, execute_memory_tool, save_memory_record
from planning import (
    GOAL_PROJECT_TOOLS,
    execute_goal_project_tool,
    format_goal_review_context,
    get_goals_due_for_review,
    mark_goals_reviewed,
)
from tasks import TASK_TOOLS, execute_task_tool
from reminders import REMINDER_TOOLS, execute_reminder_tool, is_notice_reply, reminder_context
from schedule_actions import BATCH_TOOLS, apply_schedule, schedule_context, schedule_snapshot, schedule_week_plan, verify_schedule_reply
from routines import ROUTINE_TOOLS, schedule_routine
from routine_planning import ROUTINE_PLAN_TOOLS, execute_routine_plan_tool
from execution_tools import EXECUTION_TOOLS, execute_execution_tool
from llm_limits import check_llm_available, check_llm_response
from settings import CHARACTER_PATH, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, MAX_MESSAGE_LENGTH, logger

router = APIRouter()
LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
active_chat_timings: ContextVar[dict[str, float] | None] = ContextVar('active_chat_timings', default=None)


async def post_llm_request(client: httpx.AsyncClient, *args: Any, **kwargs: Any) -> httpx.Response:
    started_at = perf_counter()
    try:
        return await client.post(*args, **kwargs)
    finally:
        timings = active_chat_timings.get()
        if timings is not None:
            timings['llm_ms'] += (perf_counter() - started_at) * 1000


DEFAULT_PERSONA = (
    'あなたはNORNという日本語のパーソナルアシスタントです。'
    '親しみがあり自然な友人のような話し方をしてください。ただし人間だと偽ったり、根拠のない親密さを演じたりしないでください。'
)
SYSTEM_PROMPT = (
    '予定やタスクを整理し、根拠と不確実性を明確にして簡潔に回答してください。'
    '既存タスクを毎日・期間の最終日まで組む依頼はschedule_routineを使い、最終日も含めて承認なしで保存してください。ただし「〜のルーティンを作って」で、既存タスクの指定がなく新しい習慣を作る依頼は「毎日」でもcreate_routine（毎日ならtarget_count=7）を使い、schedule_routineやタスク作成は併用しないでください。'
    '既存ルーティンの所要時間は引き継ぎ、不明な項目だけ尋ねてください。続く短い返答でも会話に出ている期間・項目を引き継いでschedule_routineを実行してください。'
    'ただし「全部1時間」等の明示的な時間変更は、その指定を各項目のestimated_minutesとestimate_evidenceへ反映して保存してください。以前の見積時間を理由に拒否しません。期間が既に会話に出ていれば聞き直さないでください。'
    'ユーザーが日程を任せた場合は空き時間に配置し、根拠なくハードすぎる・倒れる等と反対したり、同じ意思確認を繰り返したりしないでください。具体的な競合・空き不足・時間外だけを相談してください。'
    '予定・タスク・作業枠を区別してください。練習等のやることはタスク、その実行時間はschedule_taskで保存する作業枠、食事や会議等の時間を確保する出来事は通常の予定です。'
    'タスク本体を作っただけでカレンダーへ入れたと述べないでください。「今日やりたい」を必ず今日締切と決めつけず、実施希望日と締切を区別してください。'
    '所要時間・食事の終了時刻が不明なら尋ねてください。未指定の時間を事実として補って保存してはいけません。'
    '日程の配置・再調整は承認を求めず、apply_scheduleに具体的な操作を渡して一括で保存してください。成功した内容だけを保存済みとして説明してください。'
    '複数の予定や作業枠の再調整は、食事等の固定予定とタスク枠の移動を同じapply_scheduleにまとめてください。単独の明確な操作依頼は既存ツールで直接実行できます。'
    'タスクを複数日・複数枠へ「分けて」「振り分けて」と頼まれたら、schedule_taskで1枠だけ置かず、plan_task_allocations（日数はdistinct_days）で枠を計算してapply_scheduleで保存してください。'
    '「〜の練習のルーティンを今週分組んで」のように、既存タスクではなく回数で決まる習慣を新しく組む依頼は、まずlist_routine_plansで同名ルーティンを確認し、なければ所要時間を確認してcreate_routine（登録と今週の配置を一度に行う）を使い、タスクは作らないでください。schedule_routineは既存タスクを毎日配置する場合だけ使います。'
    'ルーティン（週N回など回数で決まる習慣）の週ごとの計画・配置はlist_routine_plansでIDを確認してplan_routine_weekを使い、未実施の見直しはreevaluate_routine_planを使ってください。'
    '「終わった」「できなかった」等の実施報告は、日付が示されていればdateを付け、なければlist_unrecorded_executionsで候補を確認してrecord_executionで実績に記録してください。候補が複数で日付が決められないときは推測で記録せず、日付と時刻を挙げて聞き返し、記録後はどの日のどの予定かを必ず伝えてください。実績の記録はタスクや予定を完了扱いにしません。'
    '誤認識された名前の訂正は同じIDのタイトル変更です。日時・所要時間・割当はそのまま維持し、古い会話の時刻へ戻さないでください。'
    'NORN内の予定の検索・追加・変更・削除は、ユーザーから明確に依頼されたら承認を求めず予定ツールで実行してください。'
    '対象や日時など実行に必要な情報が曖昧または不足している場合に限り、その情報を質問してください。'
    '「14時29分に」のような日時だけの発言を、予定検索の質問だと決めつけないでください。直前の会話の日時指定なら引き継ぎ、対象がなければ「その時間に何をする？」と作業名だけを尋ねてください。'
    '続く作業名や「追加して」は先に指定された日時と一つの依頼として扱い、未指定の日時・所要時間だけを質問してください。新しい日時が示されたら以前の希望日時より優先してください。'
    '変更・削除の対象が不明な場合は予定検索ツールを使って特定してください。'
    '予定操作の結果はツールの戻り値に従って報告し、失敗した操作を成功したと述べてはいけません。'
    'NORN内のタスク検索・追加・更新・完了・削除は、ユーザーから明確に依頼されたら承認なしでタスクツールから実行してください。'
    'さらに、ユーザー本人が実際にこれから取り組む具体的な作業について「しなきゃ」「する必要がある」「しないといけない」など必要性や実行意思を述べた場合も、登録依頼を待たずタスクとして保存してください。'
    'これは日常会話やキャラクター口調の会話でも同じです。雑談だけで終わらせず、タスクツールで保存した結果を自然な口調で短く伝えてください。'
    '作成前にlist_tasksで未完了タスクを確認し、同じ目的・対象の作業があれば新しく重複作成しないでください。後の会話で曲名や対象などが具体化したら、既存タスクをupdate_taskで具体化してください。'
    '否定、登録拒否、単なる質問、仮定、引用、他人の作業、完了済みの話、キャラクター上の架空の行動からは自動で作成・変更しないでください。「いつかできたら」など実行意思が曖昧な雑談も登録しないでください。'
    '期限や所要時間が示されていない場合は推測で設定せず、未設定のままにしてください。'
    '「今日」「明日」など日だけが明示された期限はAsia/Tokyoのその日の終わりとして扱えますが、開始時刻や所要時間は補わないでください。'
    '見積時間付きタスクを新規作成した場合は、希望開始日時があればその日時を優先し、指定がなければ期限まで（期限なしは次の5営業日以内）の空き時間を検索して最も早い候補へ自動で割り当ててください。'
    'ユーザーから特定タスクの時間割当や再割当を頼まれた場合も、空き時間を検索して最適候補へ割り当ててください。空きがなければ未割当のまま理由を伝えてください。'
    'タスクを登録したときに見積時間が不明なら、タスク本体を保存したうえで所要時間を自然に尋ねてください。開始日時を既に伝えている場合は聞き直さず、未指定の場合だけ希望を尋ねてください。返答は直前に登録したタスクの所要時間・希望日時として反映し、時間枠まで保存してください。'
    'ユーザーが時間割当は不要・後でと述べた場合は質問や割当を強制しないでください。希望日時に重複や作業時間外等の問題があれば、勝手に別時刻へ変えず理由を伝えて代案を相談してください。'
    'タスクの開始は5分刻みです。14時32分等の指定を勝手に丸めず、所要時間と希望を保持し、登録できない理由と空き時間候補を短く伝えて選択を待ってください。'
    'タスク操作の結果もツールの戻り値に従って報告してください。'
    '目標とプロジェクトの検索・追加・更新・削除は、ユーザーの明確な依頼に基づいて専用ツールで実行してください。'
    '目標を作成するときは長期的な到達状態、プロジェクトは具体的な取り組みとして区別し、プロジェクトは該当する目標へ関連付けてください。'
    '新しい進行中プロジェクトを作成したら、project_idで既存タスクを確認し、まだ無ければ最初の実行可能な作業タスクを3〜5件考えて同じproject_idに登録してください。'
    'タスクは具体的で重複しない順序にし、期限・見積時間・優先度はユーザーが示した場合だけ設定してください。目標やプロジェクトの更新では既存タスクを重複作成しないでください。'
    'タスクをプロジェクトへ関連付ける場合は、ユーザーの意図が明らかな場合だけ行ってください。'
    '会話の記憶はスレッドと別に保存されます。昔の出来事、人、好み、以前の発言について答える前に記憶検索ツールを使ってください。'
    '記憶検索のquery_variantsには、検索意図と人物・場所・時期などの具体的条件を保った言い換えだけを入れてください。'
    '検索結果は意味的に近い候補であり、事実の一致を保証しません。回答前に候補文を証拠として読み、質問が求める人物、行動、場所、時期、対象の各要素が候補に裏付けられているか判定してください。'
    '質問文に含まれる前提は記憶の証拠ではありません。候補にない具体的条件を補って答えたり、別の出来事を同じ出来事として扱ったりしないでください。'
    '一部だけ関連する記憶があれば、覚えている部分と確認できない部分を分けて伝えてください。裏付ける記憶がなければ、思い出せないと率直に伝えてください。'
    '通常の会話中に自動で記憶保存toolを呼ばないでください。自動抽出は1時間に1回、別処理でまとめて行います。ユーザーが明示的に覚えるよう依頼した場合だけ、記憶保存toolをその場で使ってください。'
    'パスワード、APIキー、認証情報、決済情報、正確な住所は記録しないでください。健康・法務・金融等の非常に慎重な情報は、ユーザーが明確に保存を依頼しない限り記録しないでください。'
    'ユーザーが内容を訂正したら対象の記憶を検索して更新し、忘れるよう頼んだら対象を特定して削除してください。保存のたびに承認を求めないでください。'
    '該当する記憶が見つからない場合は、覚えているふりをせず、そのことを率直に伝えてください。'
    'ユーザーの確認なしに外部操作を実行したと主張してはいけません。'
)
MEMORY_EXTRACTION_PROMPT = (
    '前回の抽出後に蓄積した会話から、今後の会話に役立つ長期記憶を最大3件抽出してください。'
    '対象は継続的な事実・好み・価値観と、本人にとって意味のある出来事や思い出です。'
    '一時的な予定、単発の依頼、雑談の全発言、仮定・創作・ロールプレイは記録しないでください。'
    '質問、確認、推測、以前の出来事を思い出せるか尋ねる発言は、ユーザー自身の事実として保存しないでください。'
    'assistantの発言は文脈としてのみ使い、事実の根拠はuserの発言に限定してください。'
    'パスワード、APIキー、認証情報、決済情報、正確な住所は絶対に記録しないでください。'
    '記憶は短く、後から思い出すのに十分な具体性を保ってください。'
    'source_typeはユーザーが明言した場合explicit、文脈から控えめに推定した場合inferredにしてください。'
    '各記憶に根拠となったuserメッセージのconversation_idを含めてください。'
    '適切な記憶がなければ空配列を返してください。JSON objectのみを返し、形式は'
    '{"memories":[{"category":"fact|preference|episode","content":"...","source_type":"explicit|inferred","source_conversation_id":"..."}]}です。'
)
MEMORY_CHANGE_PATTERN = re.compile(
    r'忘れて|忘れといて|忘れてください|忘れたい|覚えないで|覚えなくて|覚えておいて|覚えてね|記憶して|記録して|'
    r'記録しないで|記録しなくて|保存しないで|保存しなくて|記憶しないで|記憶しなくて|記憶から消して|記憶を削除|'
    r'訂正|修正|間違って覚え|ではなく|じゃなく|違うよ'
)
MEMORY_NON_ASSERTION_PATTERN = re.compile(
    r'[?？]|覚えてる|覚えてます|覚えてた|話したっけ|言ったっけ|だったっけ|何だっけ|なんだっけ|'
    r'思い出せる|思い出して|覚えているかな|記憶にあるかな'
)


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class AutomaticTaskIntent(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    evidence: str = Field(min_length=1, max_length=4000)
    task_id: str | None = None
    due_date: date | None = None
    due_evidence: str = ''
    preferred_date: date | None = None
    preferred_date_evidence: str = ''
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    estimate_evidence: str = ''
    start_at: datetime | None = None
    start_evidence: str = ''
    defer_scheduling: bool = False
    defer_evidence: str = ''
    continue_dialogue: bool = False


AUTOMATIC_TASK_PROMPT = (
    '会話から現実のユーザー本人がこれから行う具体的な未完了作業を判定する。会話を演じずJSONだけを返す。'
    '「しなきゃ」「する必要がある」「しないといけない」など必要性・実行意思が明確なら登録依頼なしでも対象とする。'
    '最新のuser発言を判断対象とし、直近の会話は指示語や作業対象の補完だけに使う。assistantの提案だけから作業を作らない。'
    '否定、登録拒否、単なる質問、仮定、引用、他人の作業、完了済み、願望だけの雑談、架空の演技はactionsを空にする。'
    'タスクの登録依頼や、希望日時に続く「作業名を追加して」も対象とし、先に話した希望日時を引き継いで保存する。'
    '予定・目標・プロジェクトの登録や、削除・完了などの操作はこの判定では扱わず、通常の会話処理へ任せる。'
    '食事・会議等の時間を確保する出来事は通常の予定であり、自動タスクとして登録しない。'
    'ただし直前に登録したタスクの所要時間・希望日時への返答や、既存タスクへの時間割当の依頼は、この判定で既存task_idに紐付ける。'
    '「30分」「明日の14時から」「空いている時間で」のような短い返答でも、会話から対象タスクが一意なら扱う。対象が曖昧ならactionsを空にして確認を会話側に任せる。'
    '既存タスクと同じ目的・対象ならそのtask_idを使う。単に同じ作業を言い直しただけならactionsは空。'
    '会話中の登録済み作業について曲名・対象が後で具体化した場合は既存task_idと具体化したtitleを返す。'
    '追加の場合のみtask_idをnullにする。最大3件。タイトルは実作業を短く表す。'
    'evidenceには必ず最新user発言から根拠をそのまま引用する。'
    '期限や所要時間は本人が明示した場合だけ設定し、未指定はnull。'
    '「今日やりたい」「今日がいい」は締切ではなく実施希望日。preferred_dateをYYYY-MM-DDにしpreferred_date_evidenceにuserの根拠を引用する。'
    'due_dateは「今日中に終える」「明日まで」等、締切が明示された場合だけ設定する。実施希望日は同じ依頼の直近user発言から引き継げる。'
    'due_evidenceとestimate_evidenceにはそれぞれ最新発言中の日付・時間の根拠をそのまま入れる。'
    '開始日時が明示されていればstart_atをタイムゾーン付きISO8601にし、start_evidenceに根拠を引用する。未指定の開始時刻は補わない。'
    '作業名や所要時間を後から答えた場合、直前の日時だけの発言を含め、同じ依頼について直近のuser発言で指定済みの希望日時も引き継ぐ。その根拠は該当user発言から引用する。最新の日時指定を優先する。'
    '時間割当は後で・不要と本人が述べた場合はdefer_schedulingをtrueにしdefer_evidenceへ最新発言の根拠を引用する。'
    '優先度、プロジェクト、説明、架空の予定は補わない。形式は'
    '{"actions":[{"title":"作業名","task_id":null,"evidence":"最新発言の根拠",'
    '"due_date":null,"due_evidence":"","preferred_date":null,"preferred_date_evidence":"","estimated_minutes":null,"estimate_evidence":"",'
    '"start_at":null,"start_evidence":"","defer_scheduling":false,"defer_evidence":"","continue_dialogue":false}]}。'
    '同じ最新発言に食事等の予定追加や複数対象の再調整など他の操作が含まれる場合はcontinue_dialogueをtrueにし、タスク保存だけで返答を終了させない。'
    '複数予定の再調整への了承、assistant提案だけからの時間の補完はこの判定では扱わず、actionsを空にして通常のツール経路へ任せる。'
    '名前の訂正だけなら過去の開始日時や所要時間を再設定しない。開始日時を変える明示的な依頼がある場合だけstart_atを設定する。'
)
automatic_task_lock = asyncio.Lock()
ACTION_COMPLETION_PATTERN = re.compile(
    r'(?:登録|追加|保存|変更|移動|更新|削除|解除|割り当て|割当|確保|延長|反映|設定|入れ|組み直|組みなお|組ん)'
    r'[^。！？\n]{0,28}(?:しました|ました|したよ|したから|ておいた|ておきました|ておくね|ておくよ|できた|できました|完了|済み|たよ|でおいた|でおきました|めた)'
)


def has_action_completion(text: str) -> bool:
    clauses = re.split(r'[。！？\n]|けど|ですが|ものの', text)
    return any(ACTION_COMPLETION_PATTERN.search(clause) and not re.search(r'失敗|できな|できません|していません|未登録|未反映|未確定', clause) for clause in clauses)


def split_allocation_applied_reply(proposal: dict[str, Any]) -> str | None:
    if proposal.get('state') != 'applied' or not proposal.get('ok'):
        return None
    actions = proposal.get('actions') or []
    if not actions or any(action.get('name') != 'create_task_allocation' for action in actions):
        return None
    try:
        slots = [json.loads(action['arguments_json']) for action in actions]
        task_ids = {slot['task_id'] for slot in slots}
        if len(task_ids) == 1 and len(actions) < 2:
            return None
        titles = {task['id']: task['title'] for task in         schedule_snapshot(include_inactive=True)['tasks']}
        if not task_ids <= set(titles):
            return None
        weekdays = '月火水木金土日'

        def describe(slot: dict[str, Any]) -> str:
            start = datetime.fromisoformat(slot['start_at']).astimezone(LOCAL_TIMEZONE)
            end = datetime.fromisoformat(slot['end_at']).astimezone(LOCAL_TIMEZONE)
            minutes = int((end - start).total_seconds() // 60)
            return f'{start.month}/{start.day}({weekdays[start.weekday()]}) {start:%H:%M}〜{end:%H:%M}（{minutes}分）'

        if len(task_ids) == 1:
            title = titles[next(iter(task_ids))]
            lines = [f'・{describe(slot)}' for slot in sorted(slots, key=lambda item: item['start_at'])]
            return f'「{title}」を{len(lines)}回に分けて配置したよ。\n' + '\n'.join(lines)
        sections = []
        for task_id in sorted(task_ids, key=lambda value: min(slot['start_at'] for slot in slots if slot['task_id'] == value)):
            lines = [f'  ・{describe(slot)}' for slot in sorted((slot for slot in slots if slot['task_id'] == task_id), key=lambda item: item['start_at'])]
            sections.append(f'「{titles[task_id]}」\n' + '\n'.join(lines))
        notes = ''.join(f'\n※{note}' for note in proposal.get('plan_notes') or [])
        return f'{len(task_ids)}件のタスクを優先度順に配置したよ。作業の合間に休憩も入れてあるよ。\n' + '\n'.join(sections) + notes
    except (KeyError, ValueError, TypeError):
        return None


def deterministic_action_reply(results: list[dict[str, Any]]) -> str | None:
    read_only_tools = {'list_events', 'list_tasks', 'find_task_slots', 'plan_task_allocations', 'plan_week_allocations'}
    supported_tools = {'create_event', 'update_event', 'create_task', 'update_task', 'schedule_task'}
    actions = []
    for result in results:
        tool = result.get('tool')
        if tool in read_only_tools:
            if not result.get('ok'):
                return None
            continue
        if not result.get('ok') or result.get('continue_dialogue') :
            return None
        if tool in supported_tools or tool is None and result.get('task'):
            actions.append(result)
        else:
            return None
    if len(actions) != 1:
        return None

    action = actions[0]
    tool = action.get('tool')
    summary = action.get('summary')
    if not isinstance(summary, str) or not summary.strip():
        return None
    if tool == 'update_task':
        changed_fields = set(action.get('arguments') or {}) - {'task_id'}
        if not changed_fields or not changed_fields <= {'title', 'description', 'priority', 'project_id'}:
            return None
    if tool == 'create_task' and not action.get('scheduling'):
        return None
    if tool == 'update_event' and 'recurrence' in (action.get('arguments') or {}):
        return None

    snapshot = schedule_snapshot(include_inactive=True)
    tasks_by_id = {task['id']: task for task in snapshot['tasks']}
    events_by_id = {event['id']: event for event in snapshot['events']}
    task = action.get('task')
    allocation = action.get('allocation')
    event = action.get('event')
    if tool in {'create_event', 'update_event'}:
        if not isinstance(event, dict):
            return None
        persisted = events_by_id.get(event.get('id'))
        if not persisted or persisted['title'] != event.get('title') or persisted['start_at'] != event.get('start_at') or persisted['end_at'] != event.get('end_at'):
            return None
        if persisted.get('recurrence_json') != '[]' or persisted.get('recurring_event_id'):
            return None
    elif tool == 'schedule_task':
        if not isinstance(allocation, dict):
            return None
        persisted = tasks_by_id.get(allocation.get('task_id'))
        if not persisted or any(persisted.get(key) != allocation.get(value) for key, value in (
            ('title', 'title'), ('scheduled_start', 'start_at'), ('scheduled_end', 'end_at')
        )):
            return None
    else:
        if not isinstance(task, dict):
            return None
        persisted = tasks_by_id.get(task.get('id'))
        task_fields = ('title', 'status', 'estimated_minutes', 'due_at')
        if tool is not None:
            task_fields += ('scheduled_start', 'scheduled_end')
        if not persisted or any(key not in task or persisted.get(key) != task[key] for key in task_fields):
            return None

    reply = summary.strip()
    scheduling = action.get('scheduling') or {}
    if scheduling.get('state') == 'unavailable':
        return None
    if scheduling.get('state') == 'needs_estimate':
        if (task.get('estimated_minutes') is not None
                or persisted.get('scheduled_start') is not None
                or persisted.get('scheduled_end') is not None):
            return None
        reply += ' 所要時間は何分くらいにしますか？'
        preferred_start = scheduling.get('preferred_start')
        preferred_date = scheduling.get('preferred_date')
        if preferred_start:
            reply += f' 開始希望は{preferred_start}ですね。'
        elif preferred_date:
            reply += f' 希望日は{preferred_date}ですね。'
    elif scheduling.get('state') == 'scheduled':
        start_at = scheduling.get('start_at')
        end_at = scheduling.get('end_at')
        if (not isinstance(start_at, str) or not isinstance(end_at, str)
                or persisted.get('scheduled_start') != start_at
                or persisted.get('scheduled_end') != end_at):
            return None
        if start_at not in reply:
            reply += f' 作業枠は{start_at}〜{end_at}です。'
    elif scheduling.get('state') == 'deferred':
        reply += ' 時間枠の設定は保留しています。'
    elif scheduling:
        return None
    if tool is None and not scheduling:
        return None
    return reply


def allocate_automatic_task(result, intent, latest, user_messages):
    if not result.get('ok') or not result.get('task'):
        return result
    task = result['task']
    if intent.defer_scheduling and intent.defer_evidence.strip() and intent.defer_evidence in latest:
        result['scheduling'] = {'state': 'deferred'}
        return result
    preferred = intent.start_at if (
        intent.start_at is not None and intent.start_evidence.strip()
        and any(intent.start_evidence in text for text in user_messages)
    ) else None
    if preferred is not None and preferred.tzinfo is None:
        preferred = preferred.replace(tzinfo=LOCAL_TIMEZONE)
    preferred_day = intent.preferred_date if intent.preferred_date_evidence.strip() and any(intent.preferred_date_evidence in text for text in user_messages) else None
    if task['estimated_minutes'] is None:
        result['scheduling'] = {'state': 'needs_estimate', 'preferred_start': preferred.isoformat() if preferred else None,
                                'preferred_date': preferred_day.isoformat() if preferred_day else None}
        return result
    if task.get('scheduled_start') and preferred is None and preferred_day is None:
        result['scheduling'] = {'state': 'scheduled', 'start_at': task['scheduled_start'], 'end_at': task['scheduled_end'], 'unchanged': True}
        return result
    if preferred is not None:
        start = preferred.isoformat()
    else:
        now = datetime.now(LOCAL_TIMEZONE)
        search_from = max(now, datetime.combine(preferred_day, time.min, LOCAL_TIMEZONE)) if preferred_day else now
        through = datetime.fromisoformat(task['due_at']) if task.get('due_at') else now
        if preferred_day:
            through = datetime.combine(preferred_day, time(23, 59, 59), LOCAL_TIMEZONE)
            if task.get('due_at'):
                through = min(through, datetime.fromisoformat(task['due_at']))
        elif not task.get('due_at'):
            business_days = 0
            while business_days < 5:
                through += timedelta(days=1)
                if through.weekday() < 5:
                    business_days += 1
            through = through.replace(hour=23, minute=59, second=59, microsecond=0)
        through = min(through, now + timedelta(days=31))
        if through <= search_from:
            result['scheduling'] = {'state': 'unavailable', 'error': '期限を過ぎているため時間枠を割り当てられません。'}
            return result
        available = execute_task_tool('find_task_slots', {
            'task_id': task['id'], 'from_at': search_from.isoformat(), 'through_at': through.isoformat(), 'limit': 1,
        })
        if not available.get('ok') or not available.get('slots'):
            result['scheduling'] = {'state': 'unavailable', 'error': available.get('error', '期限内に割り当てられる空き時間がありません。')}
            return result
        start = available['slots'][0]['start_at']
    scheduled = execute_task_tool('schedule_task', {'task_id': task['id'], 'start_at': start})
    if scheduled.get('ok'):
        allocation = scheduled['allocation']
        result['scheduling'] = {'state': 'scheduled', 'start_at': allocation['start_at'], 'end_at': allocation['end_at']}
        result['summary'] = result.get('summary', '') + scheduled['summary']
    else:
        result['scheduling'] = {'state': 'unavailable', 'error': scheduled.get('error'), 'preferred_start': start}
        search_start = max(datetime.fromisoformat(start), datetime.now(LOCAL_TIMEZONE))
        search_end = search_start + timedelta(days=7)
        if task.get('due_at'):
            search_end = min(search_end, datetime.fromisoformat(task['due_at']))
        if search_end > search_start:
            available = execute_task_tool('find_task_slots', {
                'task_id': task['id'], 'from_at': search_start.isoformat(),
                'through_at': search_end.isoformat(), 'limit': 3,
            })
            result['scheduling']['alternatives'] = available.get('slots', []) if available.get('ok') else []
    return result


async def prepare_automatic_tasks(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    check_llm_available()
    latest = next((message['content'] for message in reversed(messages) if message['role'] == 'user'), '')
    if not latest.strip():
        return []
    if re.search(r'提案|案(?:を|が|に)|組み直|組みなお|組み替|再調整|再計画|スケジュール|ずら|移動|その後|ご飯の後|食事の後|動か', latest):
        return []
    async with automatic_task_lock:
        existing = execute_task_tool('list_tasks', {}).get('tasks', [])
        existing = [task for task in existing if task['status'] in {'open', 'in_progress'}]
        context = {
            'now': datetime.now(LOCAL_TIMEZONE).isoformat(timespec='minutes'),
            'conversation': [message for message in messages if message['role'] in {'user', 'assistant'}][-8:],
            'existing_tasks': [{key: task[key] for key in ('id', 'title', 'due_at', 'estimated_minutes', 'scheduled_start', 'scheduled_end')}
                               for task in existing],
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10)) as client:
                check_llm_available()
                response = await post_llm_request(
                    client,
                    f'{LLM_BASE_URL}/chat/completions',
                    headers={'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {},
                    json={
                        'model': LLM_MODEL, 'temperature': 0,
                        'response_format': {'type': 'json_object'},
                        'messages': [
                            {'role': 'system', 'content': AUTOMATIC_TASK_PROMPT},
                            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)},
                        ],
                    },
                )
            check_llm_response(response)
            response.raise_for_status()
            data = json.loads(response.json()['choices'][0]['message']['content'])
            actions = data['actions']
            if not isinstance(actions, list) or len(actions) > 3:
                raise ValueError('Invalid automatic task actions')
            intents = [AutomaticTaskIntent.model_validate(action) for action in actions]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            logger.warning('Automatic task detection failed; continuing conversation without automatic changes')
            return [{'ok': False, 'error': '自動タスク判定に失敗したため自動保存は行っていません。'}]
        results = []
        by_id = {task['id']: task for task in existing}
        for intent in intents:
            if not intent.evidence.strip() or intent.evidence not in latest:
                continue
            if intent.task_id and intent.task_id not in by_id:
                continue
            arguments: dict[str, Any] = {'title': intent.title}
            if intent.due_date is not None and intent.due_evidence.strip() and intent.due_evidence in latest:
                arguments['due_at'] = datetime.combine(intent.due_date, time(23, 59, 59), LOCAL_TIMEZONE).isoformat()
            if intent.estimated_minutes is not None and intent.estimate_evidence.strip() and intent.estimate_evidence in latest:
                arguments['estimated_minutes'] = intent.estimated_minutes
            if intent.task_id:
                task = by_id[intent.task_id]
                changes = {key: value for key, value in arguments.items() if task.get(key) != value}
                result = execute_task_tool('update_task', {'task_id': intent.task_id, **changes}) if changes else {'ok': True, 'task': task}
            else:
                normalized = re.sub(r'\s+', '', intent.title).casefold()
                if any(re.sub(r'\s+', '', task['title']).casefold() == normalized for task in by_id.values()):
                    continue
                result = execute_task_tool('create_task', arguments)
            if result.get('ok') and result.get('task'):
                by_id[result['task']['id']] = result['task']
            user_messages = [message['content'] for message in messages if message['role'] == 'user'][-8:]
            if intent.task_id and set(changes) <= {'title'} and intent.start_at is None and intent.preferred_date is None and result.get('task', {}).get('scheduled_start'):
                result['scheduling'] = {'state': 'scheduled', 'start_at': result['task']['scheduled_start'], 'end_at': result['task']['scheduled_end'], 'unchanged': True}
                results.append(result)
            else:
                results.append(allocate_automatic_task(result, intent, latest, user_messages))
            if intent.continue_dialogue:
                results[-1]['continue_dialogue'] = True
        return results


def build_system_prompt() -> str:
    try:
        with CHARACTER_PATH.open('rb') as profile_file:
            contents = profile_file.read(32769)
    except FileNotFoundError:
        return DEFAULT_PERSONA + SYSTEM_PROMPT
    except OSError as error:
        logger.warning('Character profile could not be read: %s', type(error).__name__)
        raise HTTPException(status_code=503, detail='キャラクター設定を読み込めません。') from error
    try:
        if len(contents) > 32768:
            raise ValueError('Profile too large')
        profile = json.loads(contents.decode('utf-8-sig'))
        if not isinstance(profile, dict) or not profile or not all(
            isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
            for key, value in profile.items()
        ):
            raise ValueError('Expected a non-empty object of text fields')
    except (ValueError, UnicodeError) as error:
        logger.warning('Character profile is invalid: %s', type(error).__name__)
        raise HTTPException(status_code=503, detail='キャラクター設定のJSON形式を確認してください。') from error
    return (
        '以下のキャラクター設定の名前・口調・性格・会話スタイルで応答してください。'
        '通常の会話では設定や会話の裏側を自発的に説明せず、自然な台詞で応答してください。'
        'これは会話上のキャラクター設定です。架空の関係・出来事を現実のユーザーの記憶や予定として保存しないでください。'
        '予定・タスク・記憶の操作と実行結果については、続く機能上の規則を優先してください。\n'
        + json.dumps(profile, ensure_ascii=False, indent=2)
        + '\n機能上の規則:\n' + SYSTEM_PROMPT
    )


async def generate_action_reply(
    system_prompt: str,
    messages: list[dict[str, Any]],
    results: list[dict[str, Any]],
    retry: bool = True,
    allow_deterministic: bool = True,
) -> str:
    if allow_deterministic:
        simple_reply = deterministic_action_reply(results)
        if simple_reply is not None:
            return simple_reply
    check_llm_available()
    system_prompt += schedule_context()
    payload = {
        'model': LLM_MODEL,
        'temperature': 0.2,
        'messages': [
            {'role': 'system', 'content': system_prompt + (
                '\n以下のサーバー実行結果だけを根拠に、利用者への最終返答を自然な日本語で生成してください。'
                'ツールの再実行はできません。キャラクターの口調を保ち、結果の定型summaryを連結せず、短く一度だけ伝えてください。'
                'タスク本体の保存と時間枠の確定を区別してください。割当が失敗した場合は、未割当であることと理由を必ず伝えてください。'
                '既に示された日時・所要時間は聞き直さず、不足している情報だけを尋ねてください。'
                '候補は未確定として相談し、ユーザーの了承なく別時刻へ移動したと述べないでください。'
                'state=appliedのresultsは承認なしで保存済みです。保存した日時と対象を説明してください。'
                '\nサーバー実行結果:\n'
            ) + json.dumps(results, ensure_ascii=False)},
            *[message for message in messages if message['role'] in {'user', 'assistant'} and isinstance(message.get('content'), str)],
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
            response = await post_llm_request(
                client,
                f'{LLM_BASE_URL}/chat/completions', json=payload,
                headers={'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {},
            )
        check_llm_response(response)
        response.raise_for_status()
        message = response.json()['choices'][0]['message']
        content = message.get('content')
        if message.get('tool_calls') or not isinstance(content, str) or not content.strip():
            raise ValueError('Missing final reply')
        if has_action_completion(content) and not any(result.get('ok') and (result.get('summary') or result.get('task') or result.get('state') == 'applied') for result in results):
            raise ValueError('Completion without a successful operation')
        schedule_results = any(result.get('tool') in {tool['function']['name'] for tool in [*SCHEDULE_TOOLS, *TASK_TOOLS, *REMINDER_TOOLS,         *BATCH_TOOLS, *ROUTINE_TOOLS, *ROUTINE_PLAN_TOOLS, *EXECUTION_TOOLS]}
                               or result.get('task') or result.get('scheduling') or result.get('state') == 'applied' for result in results)
        if schedule_results and not await verify_schedule_reply(content, results, has_action_completion(content)):
            if retry:
                return await generate_action_reply(system_prompt + '\n前の返答は保存状態と一致しませんでした。現在のDBと実行結果を優先し、未保存・失敗を明確に説明してください。', messages, results, retry=False)
            raise ValueError('Reply does not match persisted schedule')
        return content.strip()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as error:
        logger.warning('Action reply generation failed: %s', type(error).__name__)
        failed_batches = [result for result in results if result.get('tool') in {'apply_schedule', 'schedule_routine', 'schedule_week_plan'} and not result.get('ok')]
        saved_operations = any(result.get('ok') and (result.get('summary') or result.get('task') or result.get('event') or result.get('allocation')) for result in results)
        if failed_batches and not saved_operations:
            reason = str(failed_batches[-1].get('error', '対象や期間の情報を確認してください。'))[:400]
            raise HTTPException(502, f'予定を保存できませんでした。変更は反映していません。理由: {reason}') from error
        raise HTTPException(502, 'Geminiによる結果の説明を生成できませんでした。操作を再送せず、予定・タスクの現在の状態を確認してください。') from error


async def generate_llm_reply(
    messages: list[dict[str, str]], conversation_id: str | None = None
) -> str:
    check_llm_available()
    headers = {'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {}
    local_now = datetime.now(LOCAL_TIMEZONE).isoformat(timespec='minutes')
    system_prompt = build_system_prompt()
    system_prompt += schedule_context()
    notice_context = reminder_context(conversation_id)
    latest = next((message['content'] for message in reversed(messages) if message['role'] == 'user'), '')
    routine_pattern = r'ルーティー?ン|ルーチン|routine|毎日|日課'
    routine_turn = bool(re.search(routine_pattern, latest, re.IGNORECASE))
    previous_assistant = next((message['content'] for message in reversed(messages[:-1]) if message['role'] == 'assistant'), '')
    if re.search(routine_pattern, previous_assistant, re.IGNORECASE) and re.search(r'期間|何日|いつまで', previous_assistant):
        routine_turn = routine_turn or bool(re.search(r'週間|週ぐらい|日間|まで|月末|今日から|ここから', latest))
    answering_extension = is_notice_reply(conversation_id)
    automatic_results = [] if answering_extension or routine_turn else await prepare_automatic_tasks(messages)
    automatic_context = (
        '\nサーバー側の自動タスク判定・実行結果（ユーザー発言ではありません）:\n'
        + json.dumps(automatic_results, ensure_ascii=False)
        + '\n成功した登録・更新は実行済みなので重複実行しないでください。保存結果を自然に短く伝え、失敗時や結果なしでは保存したと主張しないでください。'
        'scheduling.stateがneeds_estimateなら所要時間を自然に質問してください。preferred_startがあればその希望は把握済みなので開始日時は聞き直さないでください。'
        'scheduledなら実際の開始・終了時刻を短く伝え、再割当しないでください。'
        'unavailableならタスクの保存と時間枠の未確定を区別して理由を伝え、alternativesがあれば候補を1〜2件提案してユーザーの選択を待ってください。この返答内で勝手に別枠を取ったり再試行したりしないでください。'
        'deferredなら割当や質問を強制しないでください。'
    )
    llm_messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': f'{system_prompt}\n現在日時（Asia/Tokyo）: {local_now}{automatic_context}\n{notice_context}'},
        *messages,
    ]
    completed_actions: list[dict[str, Any]] = [result for result in automatic_results if result.get('ok') and (result.get('task') or result.get('summary'))]
    if completed_actions and not any(result.get('continue_dialogue') for result in automatic_results):
        return await generate_action_reply(llm_messages[0]['content'], messages, automatic_results)
    seen_tool_calls: set[tuple[str, str]] = set()
    tool_results: list[dict[str, Any]] = list(automatic_results)
    ungrounded_replies = 0
    schedule_turn = routine_turn or any(re.search(r'予定|スケジュール|タスク|作業枠|練習|食事|ご飯|割当|移動', message['content']) for message in messages[-8:] if message['role'] == 'user')

    for _ in range(8):
        check_llm_available()
        available_tools = [*SCHEDULE_TOOLS, *TASK_TOOLS, *MEMORY_TOOLS, *GOAL_PROJECT_TOOLS, *REMINDER_TOOLS, *BATCH_TOOLS, *ROUTINE_TOOLS, *ROUTINE_PLAN_TOOLS, *EXECUTION_TOOLS]
        if routine_turn:
            available_tools = [tool for tool in available_tools if tool['function']['name'] in {'list_events', 'list_tasks', 'find_task_slots', 'plan_task_allocations', 'plan_week_allocations',             'apply_schedule', 'schedule_week_plan', 'schedule_routine', 'list_routine_plans', 'create_routine', 'plan_routine_week', 'reevaluate_routine_plan'}]
        payload = {
            'model': LLM_MODEL,
            'messages': llm_messages,
            'tools': available_tools,
            'tool_choice': 'required' if ungrounded_replies else 'auto',
            'temperature': 0.2,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
                response = await post_llm_request(
                    client,
                    f'{LLM_BASE_URL}/chat/completions',
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException as error:
            logger.warning('LLM request timed out')
            if completed_actions:
                return await generate_action_reply(llm_messages[0]['content'], messages, tool_results)
            raise HTTPException(status_code=504, detail='LLMの応答がタイムアウトしました。') from error
        except httpx.HTTPError as error:
            logger.warning('LLM connection failed: %s', type(error).__name__)
            if completed_actions:
                return await generate_action_reply(llm_messages[0]['content'], messages, tool_results)
            raise HTTPException(status_code=502, detail='LLM接続に失敗しました。接続設定を確認してください。') from error

        check_llm_response(response)
        if response.is_error:
            logger.warning('LLM returned HTTP %s', response.status_code)
            if completed_actions:
                return await generate_action_reply(llm_messages[0]['content'], messages, tool_results)
            raise HTTPException(status_code=502, detail=f'LLMがエラーを返しました (HTTP {response.status_code})')

        try:
            assistant_message = response.json()['choices'][0]['message']
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise HTTPException(status_code=502, detail='LLMから読み取れる応答がありませんでした。') from error

        tool_calls = assistant_message.get('tool_calls') or []
        if not tool_calls:
            content = assistant_message.get('content')
            if isinstance(content, str) and content.strip():
                simple_reply = deterministic_action_reply(tool_results)
                if simple_reply is not None:
                    return simple_reply
                unsupported = has_action_completion(content) and not completed_actions
                if not unsupported and schedule_turn:
                    unsupported = not await verify_schedule_reply(content, tool_results, has_action_completion(content))
                if unsupported:
                    if ungrounded_replies:
                        return await generate_action_reply(llm_messages[0]['content'], messages, tool_results)
                    ungrounded_replies += 1
                    llm_messages.append({'role': 'system', 'content': 'まだ成功した保存操作がありません。登録・変更済みと述べてはいけません。必要なツールを実行し、不足情報があれば未実行であることを説明してください。'})
                    continue
                return content.strip()
            if completed_actions:
                return await generate_action_reply(llm_messages[0]['content'], messages, tool_results)
            raise HTTPException(status_code=502, detail='LLMから空の応答が返されました。')

        llm_messages.append({
            'role': 'assistant',
            'content': assistant_message.get('content'),
            'tool_calls': tool_calls,
        })
        batch_tool_names = {(call.get('function') or {}).get('name', '') for call in tool_calls}
        for tool_call in tool_calls:
            function = tool_call.get('function') or {}
            name = function.get('name', '')
            raw_arguments = function.get('arguments') or '{}'
            call_id = tool_call.get('id') or str(uuid.uuid4())
            call_key = (name, raw_arguments)
            arguments: dict[str, Any] = {}
            try:
                routine_created = 'create_routine' in batch_tool_names or any(
                    entry.get('tool') == 'create_routine' and entry.get('ok') for entry in tool_results
                )
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError('ツール引数はオブジェクトである必要があります。')
                mutating_tools = {
                    'create_event', 'update_event', 'delete_event',
                    'create_task', 'update_task', 'complete_task', 'delete_task',
                    'schedule_task', 'unschedule_task',
                    'extend_schedule',
                    'apply_schedule', 'schedule_week_plan',
                    'schedule_routine', 'create_routine', 'plan_routine_week', 'reevaluate_routine_plan', 'record_execution',
                    'save_memory', 'update_memory', 'forget_memory',
                    'create_goal', 'update_goal', 'delete_goal',
                    'create_project', 'update_project', 'delete_project',
                }
                if name in {'schedule_routine', 'create_task'} and routine_created:
                    result = {'ok': False, 'error': 'ルーティンを新規登録する依頼では、タスクを作らずschedule_routineも使いません。create_routineの結果だけで案内してください。'}
                elif name in mutating_tools and call_key in seen_tool_calls:
                    result = {'ok': False, 'error': '同じツール呼び出しを重複実行しませんでした。'}
                else:
                    if name in mutating_tools:
                        seen_tool_calls.add(call_key)
                    task_tools = {tool['function']['name'] for tool in TASK_TOOLS}
                    memory_tools = {tool['function']['name'] for tool in MEMORY_TOOLS}
                    goal_project_tools = {tool['function']['name'] for tool in GOAL_PROJECT_TOOLS}
                    if name in memory_tools:
                        result = await asyncio.to_thread(
                            execute_memory_tool, name, arguments, conversation_id
                        )
                    elif name in goal_project_tools:
                        result = await asyncio.to_thread(execute_goal_project_tool, name, arguments)
                    elif name == 'extend_schedule':
                        result = execute_reminder_tool(arguments)
                    elif name == 'apply_schedule':
                        result = await asyncio.to_thread(apply_schedule, arguments)
                    elif name == 'schedule_week_plan':
                        result = await asyncio.to_thread(schedule_week_plan, arguments)
                    elif name == 'schedule_routine':
                        result = await asyncio.to_thread(schedule_routine, arguments, latest)
                    elif name in {tool['function']['name'] for tool in EXECUTION_TOOLS}:
                        result = await asyncio.to_thread(execute_execution_tool, name, arguments)
                    elif name in {tool['function']['name'] for tool in ROUTINE_PLAN_TOOLS}:
                        result = await asyncio.to_thread(execute_routine_plan_tool, name, arguments)
                    elif name in task_tools:
                        result = execute_task_tool(name, arguments)
                    else:
                        result = execute_schedule_tool(name, arguments)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                result = {'ok': False, 'error': f'ツール引数が不正です: {error}'}

            tool_results.append({'tool': name, 'arguments': arguments, **result})
            if result.get('state') == 'applied' and isinstance(result.get('results'), list):
                tool_results.extend(result['results'])
                completed_actions.append(result)
                ungrounded_replies = 0
            if result.get('ok') and result.get('summary'):
                completed_actions.append(result)
                ungrounded_replies = 0
            llm_messages.append({
                'role': 'tool',
                'tool_call_id': call_id,
                'content': json.dumps(result, ensure_ascii=False),
            })
        batch_results = [result for result in tool_results if result.get('tool') in {'apply_schedule', 'schedule_week_plan', 'schedule_routine'}
                         and result.get('ok') and result.get('state') in {'applied', 'needs_input', 'unchanged'}]
        if batch_results:
            latest_batch = batch_results[-1]
            batch_children = {id(child) for batch in batch_results for child in batch.get('results') or []}
            others = [result for result in tool_results if result.get('ok') and result.get('tool') not in {'apply_schedule', 'schedule_week_plan', 'schedule_routine'}
                      and id(result) not in batch_children and (result.get('summary') or result.get('task'))]
            removals = [result for result in others if result.get('tool') == 'unschedule_task']
            others = [result for result in others if result not in removals]
            if not others:
                split_reply = split_allocation_applied_reply(latest_batch)
                if split_reply is not None:
                    return ('先に入っていた枠は解除して、' if removals else '') + split_reply
            return await generate_action_reply(build_system_prompt() + schedule_context(), messages, [*removals, *others, latest_batch])
        if any(not result.get('ok') and result.get('error') == '同じツール呼び出しを重複実行しませんでした。' for result in tool_results):
            return await generate_action_reply(llm_messages[0]['content'], messages, tool_results)

    if tool_results:
        return await generate_action_reply(llm_messages[0]['content'], messages, tool_results)
    raise HTTPException(status_code=502, detail='Geminiが予定操作を完了できませんでした。')


MEMORY_EXTRACTION_INTERVAL = timedelta(hours=1)
MEMORY_EXTRACTION_MAX_MESSAGES = 50
MEMORY_EXTRACTION_MAX_CHARACTERS = 24000


def claim_memory_extraction_batch() -> tuple[list[dict[str, Any]], int] | None:
    now = datetime.now(timezone.utc)
    attempted_at = now.isoformat(timespec='seconds')
    with closing(open_database()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        state = connection.execute(
            'SELECT last_attempted_at, last_processed_message_id FROM memory_extraction_state WHERE id = 1'
        ).fetchone()
        if state['last_attempted_at']:
            last_attempted = datetime.fromisoformat(state['last_attempted_at'])
            if now - last_attempted < MEMORY_EXTRACTION_INTERVAL:
                connection.commit()
                return None

        rows = connection.execute(
            '''
            SELECT id, conversation_id, role, content FROM messages
            WHERE id > ? ORDER BY id LIMIT ?
            ''',
            (state['last_processed_message_id'], MEMORY_EXTRACTION_MAX_MESSAGES),
        ).fetchall()
        if not rows:
            connection.commit()
            return None

        batch: list[dict[str, Any]] = []
        last_processed_id = state['last_processed_message_id']
        character_count = 0
        suppressed_assistant_conversations: set[str] = set()
        for row in rows:
            message = dict(row)
            if message['role'] == 'user' and (
                MEMORY_CHANGE_PATTERN.search(message['content'])
                or MEMORY_NON_ASSERTION_PATTERN.search(message['content'])
            ):
                suppressed_assistant_conversations.add(message['conversation_id'])
                last_processed_id = message['id']
                continue
            if message['role'] == 'assistant' and message['conversation_id'] in suppressed_assistant_conversations:
                suppressed_assistant_conversations.remove(message['conversation_id'])
                last_processed_id = message['id']
                continue
            if batch and character_count + len(message['content']) > MEMORY_EXTRACTION_MAX_CHARACTERS:
                break
            batch.append(message)
            character_count += len(message['content'])
            last_processed_id = message['id']

        if not batch:
            connection.execute(
                'UPDATE memory_extraction_state SET last_processed_message_id = ? WHERE id = 1',
                (last_processed_id,),
            )
            connection.commit()
            return None

        connection.execute(
            'UPDATE memory_extraction_state SET last_attempted_at = ? WHERE id = 1',
            (attempted_at,),
        )
        connection.commit()
    return batch, last_processed_id


def mark_memory_batch_processed(message_id: int) -> None:
    with closing(open_database()) as connection:
        connection.execute(
            '''
            UPDATE memory_extraction_state
            SET last_processed_message_id = MAX(last_processed_message_id, ?)
            WHERE id = 1
            ''',
            (message_id,),
        )
        connection.commit()


async def extract_due_memories() -> None:
    try:
        check_llm_available()
    except HTTPException:
        return
    claimed = claim_memory_extraction_batch()
    if claimed is None:
        return
    messages, last_processed_id = claimed
    user_conversation_ids = {
        message['conversation_id'] for message in messages if message['role'] == 'user'
    }
    if not user_conversation_ids:
        mark_memory_batch_processed(last_processed_id)
        return

    headers = {'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {}
    payload = {
        'model': LLM_MODEL,
        'messages': [
            {'role': 'system', 'content': MEMORY_EXTRACTION_PROMPT},
            {'role': 'user', 'content': json.dumps(messages, ensure_ascii=False)},
        ],
        'response_format': {'type': 'json_object'},
        'temperature': 0,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5)) as client:
            check_llm_available()
            response = await post_llm_request(
                client,
                f'{LLM_BASE_URL}/chat/completions', json=payload, headers=headers,
            )
        check_llm_response(response)
        if response.is_error:
            logger.warning('Memory extraction returned HTTP %s', response.status_code)
            return
        content = response.json()['choices'][0]['message']['content']
        if not isinstance(content, str):
            return
        extracted = json.loads(content)
        memories = extracted.get('memories', []) if isinstance(extracted, dict) else []
        if not isinstance(memories, list):
            return
        for item in memories[:3]:
            if not isinstance(item, dict) or set(item) != {
                'category', 'content', 'source_type', 'source_conversation_id'
            }:
                continue
            source_conversation_id = item['source_conversation_id']
            if source_conversation_id not in user_conversation_ids:
                continue
            try:
                await asyncio.to_thread(
                    save_memory_record,
                    item['category'], item['content'], item['source_type'], source_conversation_id,
                )
            except (TypeError, ValueError):
                continue
        mark_memory_batch_processed(last_processed_id)
    except HTTPException as error:
        if error.status_code != 429:
            raise
        logger.warning('Memory extraction deferred due to LLM rate limit')
    except (httpx.HTTPError, sqlite3.Error, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning('Memory extraction failed: %s', type(error).__name__)


@router.get('/api/conversations')
def list_conversations() -> list[dict[str, str]]:
    with closing(open_database()) as connection:
        rows = connection.execute(
            'SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 50'
        ).fetchall()
    return [dict(row) for row in rows]


@router.get('/api/conversations/{conversation_id}/messages')
def list_messages(conversation_id: str) -> list[dict[str, str]]:
    with closing(open_database()) as connection:
        conversation = connection.execute(
            'SELECT id FROM conversations WHERE id = ?', (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail='会話が見つかりません。')
        rows = connection.execute(
            'SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id',
            (conversation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.post('/api/chat')
async def chat(request: ChatRequest) -> dict[str, Any]:
    timings = {'llm_ms': 0.0}
    token = active_chat_timings.set(timings)
    started_at = perf_counter()
    try:
        result = await _handle_chat(request)
        server_ms = (perf_counter() - started_at) * 1000
        llm_ms = timings['llm_ms']
        result['timings_ms'] = {
            'server_ms': round(server_ms, 2),
            'llm_ms': round(llm_ms, 2),
            'processing_ms': round(max(0.0, server_ms - llm_ms), 2),
        }
        return result
    finally:
        active_chat_timings.reset(token)


async def _handle_chat(request: ChatRequest) -> dict[str, Any]:
    if not LLM_MODEL:
        raise HTTPException(
            status_code=503,
            detail='LLMが未設定です。.envでLLM_MODELと接続情報を設定してください。',
        )

    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail='メッセージを入力してください。')

    conversation_id = request.conversation_id or str(uuid.uuid4())
    try:
        uuid.UUID(conversation_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail='会話IDが不正です。') from error

    if request.conversation_id:
        with closing(open_database()) as connection:
            exists = connection.execute(
                'SELECT id FROM conversations WHERE id = ?', (conversation_id,)
            ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail='会話が見つかりません。')

    history = get_conversation_messages(conversation_id) if request.conversation_id else []
    history.append({'role': 'user', 'content': content})
    due_goal_reviews = get_goals_due_for_review()
    review_context = format_goal_review_context(due_goal_reviews)
    if review_context:
        history.insert(0, {'role': 'system', 'content': review_context})
    reply = await generate_llm_reply(history, conversation_id)
    timestamp = now_iso()

    with closing(open_database()) as connection:
        connection.execute(
            '''
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
            ''',
            (conversation_id, content[:48], timestamp, timestamp),
        )
        connection.executemany(
            'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
            [
                (conversation_id, 'user', content, timestamp),
                (conversation_id, 'assistant', reply, timestamp),
            ],
        )
        connection.commit()

    if due_goal_reviews:
        mark_goals_reviewed([goal['id'] for goal in due_goal_reviews])
    await extract_due_memories()

    return {
        'conversation_id': conversation_id,
        'user_message': {'role': 'user', 'content': content, 'created_at': timestamp},
        'assistant_message': {'role': 'assistant', 'content': reply, 'created_at': timestamp},
    }
