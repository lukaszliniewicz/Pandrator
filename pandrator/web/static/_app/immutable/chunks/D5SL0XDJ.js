import"./DsnmJJEf.js";import{p as R,d as o,n as T,r as u,a as s,b as q,s as z,f as g,v as B,g as r,t as _,e as h,c as d,u as p,C as D}from"./Ci1C937s.js";import{i as x}from"./CUHHncwq.js";import{a as E,i as F}from"./7OW89rjO.js";import{t as G}from"./4cAY3tw3.js";var b=d("<p> </p>"),H=d("<p>Automatic limits follow each subtitle track’s language.</p> <!>",1),I=d(`<div class="muted mt-2 space-y-1 text-xs" data-subtitle-profile-summary="" aria-live="polite"><!> <p>Reading speed is a target; dense speech and fixed timing can exceed it. CJK
    full-width characters count as one unit, half-width characters as half a
    unit.</p></div>`);function W(k,a){R(a,!0);const y=p(()=>Object.entries(a.profiles??{}).filter(([,e])=>e));var n=I(),w=o(n);{var L=e=>{var t=H(),l=z(g(t),2);E(l,17,()=>r(y),F,(C,j)=>{var f=p(()=>D(r(j),2));let A=()=>r(f)[0],i=()=>r(f)[1];var v=B(),J=g(v);{var K=m=>{var c=b(),N=o(c);u(c),_(O=>h(N,`${A()==="target"?"Translation":"Source"} ·
          ${O??""}:
          ${i().limits.max_chars_per_line.effective??""} units per line,
          ${a.lines??""}
          ${a.lines===1?"line":"lines"} maximum,
          ${i().limits.max_chars_per_second.effective??""} units/second target.`),[()=>["","auto","und","unknown"].includes(i().language)?"Language not yet detected":G(i().language)]),s(m,c)};x(J,m=>{i()&&m(K)})}s(C,v)}),s(e,t)},S=e=>{var t=b(),l=o(t);u(t),_(()=>h(l,`Custom: ${a.chars??""} units per line, ${a.lines??""}
      ${a.lines===1?"line":"lines"} maximum, ${a.cps??""} units/second target. Language
      changes keep these limits.`)),s(e,t)};x(w,e=>{a.automatic?e(L):e(S,-1)})}T(2),u(n),s(k,n),q()}export{W as S};
