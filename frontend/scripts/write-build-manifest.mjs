import {createHash} from 'node:crypto'
import {readdir,readFile,writeFile} from 'node:fs/promises'
import path from 'node:path'
const root=process.cwd(),files=[]
async function collect(directory){
  for(const entry of await readdir(path.join(root,directory),{withFileTypes:true})){const name=path.posix.join(directory,entry.name);if(entry.isSymbolicLink())throw new Error('Build inputs cannot be symbolic links');if(entry.isDirectory())await collect(name);else if(entry.isFile())files.push(name)}
}
for(const directory of ['src','public','scripts'])await collect(directory)
for(const entry of await readdir(root)){if(['index.html','package.json','package-lock.json','vite.config.ts'].includes(entry)||/^tsconfig.*\.json$/.test(entry))files.push(entry)}
const hashes={}
for(const name of [...new Set(files)].sort())hashes['frontend/'+name]=createHash('sha256').update(await readFile(path.join(root,name))).digest('hex')
await writeFile(path.join(root,'dist','build-meta.json'),JSON.stringify({schema_version:1,files:hashes},null,2))
process.stdout.write('Frontend build provenance recorded.\n')
