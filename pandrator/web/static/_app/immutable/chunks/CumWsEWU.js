import"./DsnmJJEf.js";import{p as O,d as o,o as R,r as u,a as s,b as T,s as z,f as v,h as B,g as r,t as _,e as h,c as d,u as p,q as D}from"./Bbzb0yXf.js";import{i as x}from"./3593izaa.js";import{e as E,i as F}from"./DL_DMBO0.js";import{t as G}from"./BrAqVj2p.js";var b=d("<p> </p>"),H=d("<p>Automatic limits follow each subtitle track’s language.</p> <!>",1),I=d(`<div class="muted mt-2 space-y-1 text-xs" data-subtitle-profile-summary="" aria-live="polite"><!> <p>Reading speed is a target; dense speech and fixed timing can exceed it. CJK
    full-width characters count as one unit, half-width characters as half a
    unit.</p></div>`);function W(k,e){O(e,!0);const y=p(()=>Object.entries(e.profiles??{}).filter(([,a])=>a));var n=I(),w=o(n);{var L=a=>{var t=H(),l=z(v(t),2);E(l,17,()=>r(y),F,(C,j)=>{var f=p(()=>D(r(j),2));let q=()=>r(f)[0],i=()=>r(f)[1];var g=B(),A=v(g);{var J=m=>{var c=b(),K=o(c);u(c),_(N=>h(K,`${q()==="target"?"Translation":"Source"} ·
          ${N??""}:
          ${i().limits.max_chars_per_line.effective??""} units per line,
          ${e.lines??""}
          ${e.lines===1?"line":"lines"} maximum,
          ${i().limits.max_chars_per_second.effective??""} units/second target.`),[()=>["","auto","und","unknown"].includes(i().language)?"Language not yet detected":G(i().language)]),s(m,c)};x(A,m=>{i()&&m(J)})}s(C,g)}),s(a,t)},S=a=>{var t=b(),l=o(t);u(t),_(()=>h(l,`Custom: ${e.chars??""} units per line, ${e.lines??""}
      ${e.lines===1?"line":"lines"} maximum, ${e.cps??""} units/second target. Language
      changes keep these limits.`)),s(a,t)};x(w,a=>{e.automatic?a(L):a(S,-1)})}R(2),u(n),s(k,n),T()}export{W as S};
