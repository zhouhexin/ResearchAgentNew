import {build} from 'esbuild'
import {mkdir} from 'node:fs/promises'
import path from 'node:path'
import {pathToFileURL} from 'node:url'
const cache=new Map()
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,String(v)),removeItem:k=>cache.delete(k)}
await mkdir('test-results',{recursive:true})
await build({entryPoints:['scripts/data-check-entry.mjs'],outfile:'test-results/data-check.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'}})
await import(pathToFileURL(path.resolve('test-results/data-check.mjs')).href)
