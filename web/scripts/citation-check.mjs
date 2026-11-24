import {build} from 'esbuild'
import {parse,compileScript} from '@vue/compiler-sfc'
import {readFile,mkdir} from 'node:fs/promises'
import path from 'node:path'
import {pathToFileURL} from 'node:url'
await mkdir('test-results',{recursive:true})
await build({entryPoints:['scripts/citation-entry.mjs'],outfile:'test-results/citations.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'},plugins:[{name:'citation-interaction',setup(b){b.onLoad({filter:/\.vue$/},async({path:file})=>{
 const {descriptor}=parse(await readFile(file,'utf8'),{filename:file})
 return{contents:compileScript(descriptor,{id:file,inlineTemplate:true}).content,loader:'ts',resolveDir:path.dirname(file)}
})}}]})
await import(pathToFileURL(path.resolve('test-results/citations.mjs')).href)
