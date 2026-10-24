// Keep browser resource URLs tied to the shipped bytes.
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const file = path.join(__dirname, 'index.html')
const before = fs.readFileSync(file, 'utf8')
const after = before.replace(/((?:src|href)="\.\/)([^"?]+)(?:\?[^" ]*)?"/g, (_, prefix, name) => {
    const target = path.resolve(__dirname, name)
    if (!target.startsWith(__dirname + path.sep)) throw new Error('Asset outside web directory')
    const hash = crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex').slice(0, 12)
    return `${prefix}${name}?v=${hash}"`
})
if (process.argv.includes('--check')) {
    if (before !== after) throw new Error('Asset versions are stale; run node web/version-assets.cjs')
} else if (before !== after) fs.writeFileSync(file, after)
