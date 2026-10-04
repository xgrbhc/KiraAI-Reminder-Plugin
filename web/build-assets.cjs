const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

const web = __dirname
const vendor = path.join(web, 'vendor')
const config = JSON.parse(fs.readFileSync(path.join(web, 'package.json'), 'utf8'))
const packages = {}
const files = new Set()

// Only developers run this build; the plugin serves committed output directly.
for (const [name, version] of Object.entries(config.devDependencies)) {
    const directory = path.join(web, 'node_modules', name)
    const installed = JSON.parse(fs.readFileSync(path.join(directory, 'package.json'), 'utf8'))
    if (installed.version !== version) throw new Error(`Run npm ci: ${name} must be ${version}`)
    packages[name] = { version, directory }
}

function write(relative, content) {
    const target = path.join(vendor, relative)
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(target, content)
    files.add(relative)
}

function copy(name, source, destination) {
    write(destination, fs.readFileSync(path.join(packages[name].directory, source)))
}

fs.mkdirSync(vendor, { recursive: true })
execFileSync(process.execPath, [
    path.join(packages.tailwindcss.directory, 'lib', 'cli.js'),
    '--config', path.join(web, 'tailwind.config.cjs'),
    '--input', path.join(web, 'tailwind.input.css'),
    '--output', path.join(vendor, 'tailwind.min.css'),
    '--minify',
], { cwd: web, stdio: 'inherit' })
files.add('tailwind.min.css')
copy('tailwindcss', 'LICENSE', 'licenses/tailwindcss.txt')
copy('vue', 'dist/vue.global.prod.js', 'vue.global.prod.js')
copy('vue', 'LICENSE', 'licenses/vue.txt')

// Preserve upstream CSS and font fallbacks, shipping only the two used styles.
const iconPackage = '@fortawesome/fontawesome-free'
const iconCss = ['fontawesome.min.css', 'solid.min.css', 'regular.min.css']
    .map(name => fs.readFileSync(path.join(packages[iconPackage].directory, 'css', name), 'utf8'))
    .join('\n')
write('fontawesome/css/icons.min.css', iconCss)
for (const match of iconCss.matchAll(/url\(\.\.\/webfonts\/([^)]*)\)/g)) {
    copy(iconPackage, `webfonts/${match[1]}`, `fontawesome/webfonts/${match[1]}`)
}
copy(iconPackage, 'LICENSE.txt', 'licenses/fontawesome-free.txt')

const fontPackage = '@fontsource/outfit'
const fontCss = [300, 400, 500, 600, 700]
    .map(weight => fs.readFileSync(path.join(packages[fontPackage].directory, `${weight}.css`), 'utf8'))
    .join('\n')
write('outfit/outfit.css', fontCss)
for (const match of fontCss.matchAll(/url\(\.\/files\/([^)]*)\)/g)) {
    copy(fontPackage, `files/${match[1]}`, `outfit/files/${match[1]}`)
}
copy(fontPackage, 'LICENSE', 'licenses/outfit.txt')

const manifest = {
    packages: Object.fromEntries(Object.entries(packages).map(([name, entry]) => [name, entry.version])),
    files: Object.fromEntries([...files].sort().map(relative => [relative, {
        bytes: fs.statSync(path.join(vendor, relative)).size,
        sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(vendor, relative))).digest('hex'),
    }])),
}
fs.writeFileSync(path.join(vendor, 'asset-manifest.json'), JSON.stringify(manifest, null, 2) + '\n')
console.log(`Built ${files.size} local assets (${Object.values(manifest.files).reduce((sum, file) => sum + file.bytes, 0)} bytes).`)
