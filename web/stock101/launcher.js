/* Uses AIRI's native window bridge when available; browser fallback stays separate. */
(()=>{
 let pending;
 const matches=text=>/stock\s*101|스톡\s*101/i.test(text)&&/open|launch|열어|띄워|켜줘/i.test(text)&&!/don't|do not|하지\s*마|열지\s*마/i.test(text);
 function open(){
  const native=window.webkit?.messageHandlers?.airiDesktop;
  if(native){native.postMessage({kind:'stock101_open'});return true;}
  const win=window.open('/stock101','Stock101','width=1320,height=880,resizable=yes,scrollbars=yes');
  if(win){win.focus();return true;}
  if(typeof toast==='function')toast('Open Stock101 in a separate window',()=>window.open('/stock101','Stock101','width=1320,height=880'));
  return false;
 }
 window.Stock101={open,matches};
 const rail=document.querySelector('.companion-rail')||document.querySelector('nav');
 if(rail){const b=document.createElement('button');b.textContent='↗ Stock101';b.className='nav';b.title='Open Stock101 in a separate window';b.onclick=open;rail.append(b);}
})();
