import test from 'node:test'
import assert from 'node:assert/strict'
import { UtteranceBuffer, VoiceSession, isConversationEnd, stripWakePhrase } from '../src/voiceSession.ts'

test('waiting audio is never sent as an utterance', () => {
	const buffer = new UtteranceBuffer()
	buffer.push(new Float32Array([0.9]), false)
	buffer.begin()
	buffer.push(new Float32Array([0.2]), true)
	assert.deepEqual(buffer.take(), new Float32Array([0.2]))
})

test('active preroll and utterance length are bounded', () => {
	const buffer = new UtteranceBuffer()
	for (let index = 0; index < 30; index++) buffer.push(new Float32Array([index]), true)
	buffer.begin()
	assert.equal(buffer.take().length, 20)
	buffer.begin()
	buffer.push(new Float32Array(16000 * 30), true)
	assert.equal(buffer.push(new Float32Array(1), true), false)
	assert.equal(buffer.take().length, 0)
})

test('one wake enables continuous conversation with a 60-second timeout', () => {
	const session = new VoiceSession()
	session.standby(0)
	session.tick(120000, false, false)
	assert.equal(session.active, false)
	assert.equal(session.wake(120000), true)
	assert.equal(session.wake(120001), false)
	session.activity(150000)
	session.tick(209999, false, false)
	assert.equal(session.phase, 'listening')
	session.tick(210000, false, false)
	assert.equal(session.phase, 'standby')
})

test('processing and playback do not consume idle time', () => {
	const session = new VoiceSession()
	session.standby(0)
	session.wake(0)
	session.tick(90000, true, false)
	session.tick(180000, false, true)
	session.tick(239999, false, false)
	assert.equal(session.phase, 'listening')
	session.tick(240000, false, false)
	assert.equal(session.phase, 'standby')
})

test('stop phrases match only a complete utterance', () => {
	assert.equal(isConversationEnd('待機して。'), true)
	assert.equal(isConversationEnd('またね！'), true)
	assert.equal(isConversationEnd('またねって言ってた'), false)
})

test('Nee Norn is stripped without losing the following command', () => {
	assert.equal(stripWakePhrase('ねぇノルン、今日の予定は？'), '今日の予定は？')
	assert.equal(stripWakePhrase('ねえ、のるん。こんにちは'), 'こんにちは')
	assert.equal(stripWakePhrase('ねー Norn, こんにちは'), 'こんにちは')
	assert.equal(stripWakePhrase('ねぇノルン'), '')
	assert.equal(stripWakePhrase('ノルン、今日の予定は？'), 'ノルン、今日の予定は？')
	assert.equal(stripWakePhrase('Norn, こんにちは'), 'Norn, こんにちは')
	assert.equal(stripWakePhrase('Hey Emma, こんにちは'), 'Hey Emma, こんにちは')
})
