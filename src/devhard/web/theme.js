'use strict';
(() => {
  const registry=[
    {id:'light', label:'Default Light', appearance:'light'},
    {id:'devhard-light', label:'DevHard Light', appearance:'light'},
    {id:'solarized-light', label:'Solarized Light', appearance:'light'},
    {id:'catppuccin-latte', label:'Catppuccin Latte', appearance:'light'},
    {id:'rose-pine-dawn', label:'Rosé Pine Dawn', appearance:'light'},
    {id:'dark', label:'Default Dark', appearance:'dark'},
    {id:'devhard', label:'DevHard', appearance:'dark'},
    {id:'solarized-dark', label:'Solarized Dark', appearance:'dark'},
    {id:'nord', label:'Nord', appearance:'dark'},
    {id:'gruvbox', label:'Gruvbox', appearance:'dark'},
    {id:'monokai', label:'Monokai', appearance:'dark'},
    {id:'catppuccin-mocha', label:'Catppuccin Mocha', appearance:'dark'},
    {id:'tokyo-night', label:'Tokyo Night', appearance:'dark'},
    {id:'rose-pine-moon', label:'Rosé Pine Moon', appearance:'dark'},
    {id:'dracula', label:'Dracula', appearance:'dark'}
  ];
  const byId=new Map(registry.map(scheme=>[scheme.id, scheme]));
  const defaults={light:'light', dark:'dark'};
  const modes=new Set(['system', 'light', 'dark']);
  const modeKey='devhard.theme.mode';
  const styleKey='devhard.theme.style';
  const styles=new Set(['round', 'flat', 'terminal']);
  const schemeKey=appearance=>'devhard.theme.'+appearance;
  const media=window.matchMedia('(prefers-color-scheme: dark)');
  const store=(key,value) => { try { localStorage.setItem(key,value); } catch {} };
  const load=key => { try { return localStorage.getItem(key); } catch { return null; } };
  const schemesFor=appearance => registry.filter(scheme=>scheme.appearance===appearance);
  const readMode=() => { const value=load(modeKey); return modes.has(value) ? value : 'system'; };
  const systemAppearance=() => media.matches ? 'dark' : 'light';
  const effectiveAppearance=() => { const mode=readMode(); return mode==='system' ? systemAppearance() : mode; };
  const readScheme=appearance => {
    const scheme=byId.get(load(schemeKey(appearance)));
    return scheme && scheme.appearance===appearance ? scheme.id : defaults[appearance];
  };
  const apply=() => { document.documentElement.dataset.theme=readScheme(effectiveAppearance()); };
  const setMode=mode => { if(modes.has(mode)) { store(modeKey,mode); apply(); } };
  const setScheme=(appearance,id) => {
    const scheme=byId.get(id);
    if(!scheme || scheme.appearance!==appearance) return;
    store(schemeKey(appearance),id); apply();
  };
  const readStyle=() => { const value=load(styleKey); return styles.has(value) ? value : 'round'; };
  const applyStyle=() => { document.documentElement.dataset.style=readStyle(); };
  const setStyle=style => { if(styles.has(style)) { store(styleKey,style); applyStyle(); } };
  const notify=() => window.dispatchEvent(new Event('devhard:appearance'));
  media.addEventListener('change', () => { if(readMode()==='system') { apply(); notify(); } });
  window.DevHardTheme={registry, modeKey, schemeKey, styleKey, schemesFor, readMode, effectiveAppearance, readScheme, readStyle, setMode, setScheme, setStyle, apply, applyStyle};
  apply();
  applyStyle();
})();
