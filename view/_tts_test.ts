import assert from 'node:assert'
import { splitChunks } from './src/composables/useTTS'

let pass = 0
function ok(name: string, cond: boolean, extra = '') {
  if (!cond) {
    console.error(`✗ ${name} ${extra}`)
    process.exitCode = 1
    return
  }
  pass++
  console.log(`✓ ${name}`)
}

// 1. 短文本不会被切碎
const short = splitChunks('你好。今天天气不错。')
ok('短文本合为一段', short.length === 1, `实际=${JSON.stringify(short)}`)

// 2. 长文本按 80 字上限切，且内容不丢
const long = '这是一句测试用的话。'.repeat(30)
const parts = splitChunks(long)
ok('长文本被切成多段', parts.length > 1, `段数=${parts.length}`)
ok('每段不超过 80 字', parts.every((s) => s.length <= 80), `最长=${Math.max(...parts.map((s) => s.length))}`)
ok('切分不丢字', parts.join('') === long.replace(/\s/g, ''), `原${long.length} 拼回${parts.join('').length}`)

// 3. 无标点长文走硬切，不会塞成一段
const noPunc = '啊'.repeat(300)
const hard = splitChunks(noPunc)
ok('无标点长文被切段', hard.length === 4, `段数=${hard.length}`)
ok('硬切也不超上限', hard.every((s) => s.length <= 80))

// 4. 代码块整块丢弃，不留半截
const code = '先看代码：\n```js\nconst a = 1\nconsole.log(a)\n```\n就这样。'
const withCode = splitChunks(code)
ok('代码块被丢掉', !withCode.join('').includes('console.log'), `实际=${JSON.stringify(withCode)}`)
ok('代码块前后的文字还在', withCode.join('').includes('先看代码'))

// 5. 边界
ok('空字符串返回空数组', splitChunks('').length === 0)
ok('纯空白返回空数组', splitChunks('   \n\n  ').length === 0)
ok('只有代码块返回空数组', splitChunks('```\nfoo\n```').length === 0)

// 6. 中英混排不炸
const mixed = '这是中文。This is a longer English sentence that keeps going without stop. 又是中文。'
const mixParts = splitChunks(mixed)
ok('中英混排能切开', mixParts.length >= 1 && mixParts.every((s) => s.length <= 80))

console.log(`\n${pass} 项通过`)
