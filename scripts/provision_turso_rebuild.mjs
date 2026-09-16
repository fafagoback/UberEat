const token=process.env.TURSO_PLATFORM_TOKEN;
const targetName=process.env.TURSO_REBUILD_NAME||'ubereats-rebuilt-v10';
const resetTarget=process.env.TURSO_RESET_TARGET==='1';
const databaseUpload=process.env.TURSO_DATABASE_UPLOAD==='1';
const currentHost=String(process.env.TURSO_DATABASE_URL||'').replace(/^libsql:\/\//,'').replace(/^https?:\/\//,'').split('/')[0];
if(!token) throw new Error('TURSO_PLATFORM_TOKEN is required');

const headers={Authorization:`Bearer ${token}`,'Content-Type':'application/json'};
const api=async(path,options={})=>{
  const response=await fetch(`https://api.turso.tech/v1${path}`,{...options,headers:{...headers,...options.headers}});
  if(!response.ok){const body=await response.text();const error=new Error(`${options.method||'GET'} ${path}: ${response.status} ${body}`);error.status=response.status;throw error;}
  const text=await response.text();return text?JSON.parse(text):{};
};

const organizations=await api('/organizations');
let selected=null;
for(const organization of organizations){
  const listing=await api(`/organizations/${encodeURIComponent(organization.slug)}/databases`);
  const current=(listing.databases||[]).find(database=>database.Hostname===currentHost);
  if(current){selected={organization,group:current.group||'default'};break;}
}
// The source database may already have been deleted. Its hostname still ends
// with the owning organization slug, so use that stable part as a fallback.
if(!selected&&currentHost){
  const owner=organizations.find(organization=>
    currentHost===`${organization.slug}.turso.io`||
    currentHost.endsWith(`-${organization.slug}.turso.io`));
  if(owner){
    selected={organization:owner,group:'default'};
    console.log(`Selected organization ${owner.slug} from the deleted database hostname`);
  }
}
if(!selected){
  if(organizations.length!==1) throw new Error('Unable to select Turso organization from the database hostname');
  selected={organization:organizations[0],group:'default'};
}

const slug=selected.organization.slug;
let database;
try{
  const result=await api(`/organizations/${encodeURIComponent(slug)}/databases/${encodeURIComponent(targetName)}`);
  database=result.database;
  if(resetTarget){
    await api(`/organizations/${encodeURIComponent(slug)}/databases/${encodeURIComponent(targetName)}`,{method:'DELETE'});
    console.log(`Deleted existing isolated rebuild target: ${targetName}`);
    database=null;
  }
}catch(error){
  if(error.status!==404) throw error;
}

if(!database){
  const result=await api(`/organizations/${encodeURIComponent(slug)}/databases`,{
    method:'POST',body:JSON.stringify({
      name:targetName,
      group:selected.group,
      ...(databaseUpload?{seed:{type:'database_upload'}}:{})
    })
  });
  database=result.database;
  console.log(`Created isolated rebuild target: ${targetName}${databaseUpload?' (database upload)':''}`);
}

const auth=await api(`/organizations/${encodeURIComponent(slug)}/databases/${encodeURIComponent(targetName)}/auth/tokens?authorization=full-access`,{method:'POST'});
const url=`libsql://${database.Hostname}`;
console.log(`::add-mask::${auth.jwt}`);
const output=process.env.GITHUB_OUTPUT;
if(!output) throw new Error('GITHUB_OUTPUT is required');
const fs=await import('node:fs');
fs.appendFileSync(output,`database_name=${targetName}\ndatabase_url=${url}\nauth_token=${auth.jwt}\norganization=${slug}\n`);
if(process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY,`\n### Isolated Turso rebuild target\n- Organization: \`${slug}\`\n- Database: \`${targetName}\`\n- URL: \`${url}\`\n`);
