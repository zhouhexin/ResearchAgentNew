import {test} from 'node:test'
import assert from 'node:assert/strict'
import {parseCitation,safeUrl,validYears,csvCell,cleanParams} from '../src/api/utils.ts'
test('移除空筛选，保留 false、年份和分页',()=>{const params={q:'',year:2026,has_fulltext:false,has_analysis:'',page:1,page_size:20,unused:null,missing:undefined,zero:0};assert.deepEqual(cleanParams(params),{year:2026,has_fulltext:false,page:1,page_size:20,zero:0});assert.equal(params.has_analysis,'');assert.equal(cleanParams(undefined),undefined)})
test('证据编号支持单数字和多数字论文 ID',()=>{assert.deepEqual(parseCitation('P2:E21'),{paperId:2,evidenceId:'E21'});assert.deepEqual(parseCitation('E135'),{paperId:undefined,evidenceId:'E135'});assert.deepEqual(parseCitation('P12:E135'),{paperId:12,evidenceId:'E135'})})
test('拒绝无效与前导零引用',()=>{for(const r of ['P0:E1','P02:E1','E0','E01','P2:E21<script>','P2:'])assert.equal(parseCitation(r),null)})
test('来源只允许 http/https',()=>{for(const u of ['javascript:alert(1)','file:///etc/passwd','data:text/html,test','本地导入 PDF'])assert.equal(safeUrl(u),null);assert.equal(safeUrl('https://doi.org/10.1234/test'),'https://doi.org/10.1234/test')})
test('年份起止关系',()=>{assert.equal(validYears(2025,2020),false);assert.equal(validYears(undefined,2025),true)})
test('CSV 转义与公式防护',()=>{assert.equal(csvCell('a,"b"'),'"a,""b"""');assert.equal(csvCell('=1+1'),'"\'=1+1"')})
