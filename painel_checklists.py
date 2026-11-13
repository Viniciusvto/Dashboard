"""
Painel de Checklists GBMX — versão em um único script (Tkinter).

Baixa os checklists do GitHub (AutomationGBMX/checklists-gt, pasta checklists/),
guarda uma cópia na pasta "dados_checklists" ao lado deste arquivo e mostra o painel.
Sem internet, mostra os últimos dados baixados.
Lê só os checklists com JSON; os antigos que só têm PDF são ignorados.
Não precisa instalar nada além do Python.

Variáveis de ambiente opcionais:
    GITHUB_TOKEN           (só se o repositório for privado)
    INTERVALO_SEGUNDOS     (padrão: 30)
"""

import base64
import hashlib
import json
import os
import queue
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk
from tkinter import font as tkfont

# ============================================================
#  CONFIGURAÇÕES
# ============================================================

REPOSITORIO = "AutomationGBMX/checklists-gt"
BRANCH = "main"
PASTA_REMOTA = "checklists"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
INTERVALO_SEGUNDOS = max(10, int(os.getenv("INTERVALO_SEGUNDOS", "30")))
PASTA_DADOS = Path(__file__).resolve().parent / "dados_checklists"

VERDE_ESCURO = "#007330"
VERDE = "#008B39"
LIMAO = "#9FC63B"
VERMELHO = "#D03B3B"
TEXTO = "#1F2A24"
FUNDO = "#F4F5F3"
BORDA = "#DDE3DD"
SUAVE = "#6B7A70"
FONTE = "Segoe UI"

STATUS = {"APROVADO": "✅ APROVADO", "REPROVADO": "❌ REPROVADO"}

ROTULOS_ITENS = {
    1: "Leitor de crachá",
    2: "Botões / emergência / painel",
    3: "Sirene",
    4: "Condições gerais do equipamento",
    5: "Fixação ao solo",
    6: "Corda de tracionamento",
    7: "Corrente e amarração",
    8: "Carga dentro do limite",
    9: "Demarcação no solo",
    10: "Treinamento do operador",
    11: "Liberação final segura",
}


def agora():
    return datetime.now().strftime("%H:%M:%S")


# ============================================================
#  SINCRONIZAÇÃO COM O GITHUB (sem git)
# ============================================================

class LimiteGitHub(Exception):
    pass


def _baixar_url(url, cabecalhos=None, timeout=20):
    pedido = urllib.request.Request(url, headers={"User-Agent": "PainelGBMX", **(cabecalhos or {})})
    with urllib.request.urlopen(pedido, timeout=timeout) as resp:
        return resp.read()


def _sha_blob(dados):
    return hashlib.sha1(b"blob %d\0" % len(dados) + dados).hexdigest()


def _gravar_atomico(destino, dados):
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.write_bytes(dados)
    os.replace(tmp, destino)


class Sincronizador:
    """Mantém PASTA_DADOS/checklists igual à pasta checklists/ do GitHub."""

    def __init__(self):
        self.pasta = PASTA_DADOS / "checklists"
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.arquivo_estado = PASTA_DADOS / "estado.json"
        try:
            self.estado = json.loads(self.arquivo_estado.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.estado = {}
        self.bloqueado_ate = 0.0

    def _salvar_estado(self):
        _gravar_atomico(self.arquivo_estado, json.dumps(self.estado, indent=1).encode("utf-8"))

    def _api(self, caminho, **cabecalhos):
        if GITHUB_TOKEN:
            cabecalhos["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        try:
            return _baixar_url(f"https://api.github.com/repos/{REPOSITORIO}{caminho}", cabecalhos)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and (e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Remaining") == "0"):
                if e.headers.get("Retry-After"):
                    self.bloqueado_ate = time.time() + int(e.headers["Retry-After"])
                else:
                    self.bloqueado_ate = float(e.headers.get("X-RateLimit-Reset", time.time() + 60))
                raise LimiteGitHub("Limite de consultas do GitHub atingido até "
                                   f"{datetime.fromtimestamp(self.bloqueado_ate):%H:%M:%S}.")
            raise

    def _commit_remoto(self):
        # Mesmo endereço usado pelo "git fetch": não gasta o limite de 60 consultas/hora da API.
        cabecalhos = {}
        if GITHUB_TOKEN:
            cabecalhos["Authorization"] = "Basic " + base64.b64encode(f"x-access-token:{GITHUB_TOKEN}".encode()).decode()
        dados = _baixar_url(f"https://github.com/{REPOSITORIO}.git/info/refs?service=git-upload-pack", cabecalhos)
        alvo = f" refs/heads/{BRANCH}".encode()
        for linha in dados.split(b"\n"):
            linha = linha.split(b"\0")[0]
            if linha.endswith(alvo):
                return linha[-len(alvo) - 40:-len(alvo)].decode()
        raise ValueError(f"Branch {BRANCH} não encontrada no GitHub.")

    def _arquivos_remotos(self, commit):
        arvore = json.loads(self._api(f"/git/trees/{commit}?recursive=1"))
        prefixo = PASTA_REMOTA + "/"
        return {
            item["path"][len(prefixo):]: item["sha"]
            for item in arvore["tree"]
            if item["type"] == "blob" and item["path"].startswith(prefixo)
            and "/" not in item["path"][len(prefixo):] and item["path"].endswith((".json", ".pdf"))
        }

    def _baixar_arquivo(self, commit, nome, sha):
        if GITHUB_TOKEN:
            dados = self._api(f"/git/blobs/{sha}", Accept="application/vnd.github.raw")
        else:
            dados = _baixar_url(f"https://raw.githubusercontent.com/{REPOSITORIO}/{commit}/{PASTA_REMOTA}/{nome}", timeout=60)
        if _sha_blob(dados) != sha:
            raise ValueError(f"{nome}: arquivo baixado não confere com o GitHub.")
        return dados

    def sincronizar(self):
        """Retorna (online, mudou, erro)."""
        if time.time() < self.bloqueado_ate:
            return False, False, "Limite de consultas do GitHub atingido; aguardando."
        try:
            commit = self._commit_remoto()
            conhecidos = self.estado.get("arquivos", {})
            completos = all((self.pasta / n).is_file() for n in conhecidos)
            if commit == self.estado.get("commit") and completos:
                return True, False, None

            remotos = self._arquivos_remotos(commit)
            alterou = False
            for nome, sha in sorted(remotos.items()):
                if conhecidos.get(nome) != sha or not (self.pasta / nome).is_file():
                    _gravar_atomico(self.pasta / nome, self._baixar_arquivo(commit, nome, sha))
                    conhecidos[nome] = sha
                    self.estado["arquivos"] = conhecidos
                    self._salvar_estado()
                    alterou = True
            for local in self.pasta.iterdir():
                if local.name not in remotos:
                    local.unlink()
                    alterou = True

            self.estado.update(commit=commit, arquivos=remotos)
            self._salvar_estado()
            return True, alterou, None
        except LimiteGitHub as e:
            return False, False, str(e)
        except urllib.error.HTTPError as e:
            if e.code in (401, 404):
                return False, False, "Repositório não encontrado ou sem permissão (confira o GITHUB_TOKEN)."
            return False, False, f"GitHub respondeu com erro {e.code}."
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return False, False, "Sem conexão com o GitHub."
        except (ValueError, KeyError, OSError) as e:
            return False, False, str(e)[:200]


# ============================================================
#  LEITURA DOS CHECKLISTS (JSON)
# ============================================================

def ler_checklists(pasta):
    """Retorna (checklists, respostas_por_id, erros); checklists do mais novo para o mais antigo."""
    checklists, respostas, erros = [], {}, []
    for arquivo in sorted(pasta.glob("checklist_*.json")):
        try:
            reg = json.loads(arquivo.read_text(encoding="utf-8"))
            data_hora = datetime.fromisoformat(reg["data_hora"])
            itens = [{
                "item": r["item"],
                "rotulo": ROTULOS_ITENS.get(r["item"], r["pergunta"][:40]),
                "pergunta": r["pergunta"],
                "resposta": r["resposta"],
            } for r in reg["respostas"]]
            checklists.append({
                "id": reg["id"],
                "data_hora": data_hora,
                "operador": reg["operador"],
                "matricula": reg.get("matricula", ""),
                "status": reg["status"],
                "itens_nao_ok": sum(r["resposta"] != "OK" for r in itens),
                "pdf": arquivo.with_suffix(".pdf").name,
            })
            respostas[reg["id"]] = sorted(itens, key=lambda r: r["item"])
        except Exception as e:
            erros.append(f"{arquivo.name}: {e}")
    checklists.sort(key=lambda c: c["data_hora"], reverse=True)
    return checklists, respostas, erros


# ============================================================
#  THREAD DE SINCRONIZAÇÃO
# ============================================================

def loop_sincronizacao(sinc, fila, parar):
    while not parar.is_set():
        online, mudou, erro = sinc.sincronizar()
        if mudou:
            fila.put(("dados", ler_checklists(sinc.pasta)))
        fila.put(("estado", online, erro, agora()))
        parar.wait(INTERVALO_SEGUNDOS)


# ============================================================
#  GUI (Tkinter)
# ============================================================

class PainelChecklists(tk.Tk):
    PERIODOS = {"Tudo": None, "Hoje": 0, "Últimos 7 dias": 6, "Últimos 30 dias": 29}

    def __init__(self, sinc, fila, parar):
        super().__init__()
        self.sinc, self.fila, self.parar = sinc, fila, parar
        self.checklists, self.respostas, self.erros = [], {}, []
        self.ids_conhecidos = None
        self.operadores_vars = {}
        self.filtrados = []
        self.selecionado = None
        self.dados_dias, self.dados_itens = [], []

        self.title("Painel de Checklists GBMX")
        self.escala = self.winfo_fpixels("1i") / 96
        altura_util = self.winfo_screenheight() / self.escala - 80
        self.fator = max(0.68, min(1.6, altura_util / 820))
        self._fontes = {}
        self.geometry(f"{self.px(1360)}x{self.px(860)}")
        self.minsize(self.px(900), self.px(560))
        self.configure(bg=FUNDO)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.bind("<F11>", lambda e: self.attributes("-fullscreen", not self.attributes("-fullscreen")))

        self._estilos()
        self._montar_layout()
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)
        self._receber_dados(ler_checklists(sinc.pasta))
        self._processar_fila()

    # ---------- layout ----------

    def px(self, valor):
        return int(valor * self.escala * getattr(self, "fator", 1.0))

    def fonte(self, tamanho, peso="normal"):
        chave = (tamanho, peso)
        if chave not in self._fontes:
            self._fontes[chave] = tkfont.Font(family=FONTE, size=max(7, round(tamanho * self.fator)), weight=peso)
        return self._fontes[chave]

    def _estilos(self):
        estilo = ttk.Style(self)
        estilo.theme_use("clam")
        estilo.configure("Treeview", font=self.fonte(10), rowheight=self.px(26), background="white",
                         fieldbackground="white", foreground=TEXTO, bordercolor=BORDA)
        estilo.configure("Treeview.Heading", font=self.fonte(10, "bold"), background="#F0F4F1",
                         foreground=TEXTO, relief="flat")
        estilo.map("Treeview", background=[("selected", "#CFE8D6")], foreground=[("selected", TEXTO)])
        estilo.configure("TCombobox", padding=5)

    def _secao(self, pai, texto):
        caixa = tk.Frame(pai, bg=LIMAO)
        tk.Label(caixa, text=texto, bg=VERDE, fg="white", font=self.fonte(11, "bold"),
                 padx=12, pady=4).pack(padx=(6, 0))
        return caixa

    def _montar_layout(self):
        raiz = tk.Frame(self, bg=FUNDO)
        raiz.pack(fill="both", expand=True, padx=self.px(18), pady=(self.px(12), self.px(8)))

        cab = tk.Frame(raiz, bg=VERDE_ESCURO, padx=self.px(24), pady=self.px(12))
        cab.pack(fill="x")
        textos = tk.Frame(cab, bg=VERDE_ESCURO)
        textos.pack(side="left")
        tk.Label(textos, text="Controle de Checklists de Segurança", bg=VERDE_ESCURO, fg="white",
                 font=self.fonte(20, "bold")).pack(anchor="w")
        tk.Label(textos, text="Guincho Tracionador 1172 · validação de equipamento antes da operação",
                 bg=VERDE_ESCURO, fg="#DDEFD9", font=self.fonte(11)).pack(anchor="w")
        logo = tk.Frame(cab, bg=VERDE_ESCURO)
        logo.pack(side="right")
        tk.Label(logo, text="GBMX", bg=VERDE_ESCURO, fg=LIMAO, font=self.fonte(24, "bold")).pack(anchor="e")
        tk.Label(logo, text="GREENBRIER MAXION", bg=VERDE_ESCURO, fg="white", font=self.fonte(7, "bold")).pack(anchor="e")
        tk.Frame(raiz, bg=LIMAO, height=6).pack(fill="x", pady=(0, 6))

        self.lbl_legenda = tk.Label(raiz, text="Carregando…", bg=FUNDO, fg=SUAVE, font=self.fonte(9), anchor="w")
        self.lbl_legenda.pack(fill="x")
        self.lbl_offline = tk.Label(raiz, bg="#FFF6E0", fg=TEXTO, font=self.fonte(10), anchor="w",
                                    padx=12, pady=6, highlightbackground="#F0D58A", highlightthickness=1)
        self.ancora_avisos = tk.Frame(raiz, bg=FUNDO)
        self.ancora_avisos.pack(fill="x")

        filtros = tk.Frame(raiz, bg=FUNDO)
        filtros.pack(fill="x", pady=(self.px(4), self.px(8)))
        tk.Label(filtros, text="Período", bg=FUNDO, fg=TEXTO, font=self.fonte(9, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(filtros, text="Operador", bg=FUNDO, fg=TEXTO, font=self.fonte(9, "bold")).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self.periodo = ttk.Combobox(filtros, values=list(self.PERIODOS), state="readonly", width=22, font=self.fonte(10))
        self.periodo.set("Tudo")
        self.periodo.bind("<<ComboboxSelected>>", lambda e: self.atualizar())
        self.periodo.grid(row=1, column=0, sticky="w")
        self.btn_operador = tk.Menubutton(filtros, text="Todos os operadores ▾", bg="white", fg=TEXTO,
                                          font=self.fonte(10), anchor="w", relief="solid", bd=1, width=60,
                                          padx=8, pady=3, highlightthickness=0)
        self.menu_operador = tk.Menu(self.btn_operador, tearoff=False, font=self.fonte(10))
        self.btn_operador["menu"] = self.menu_operador
        self.btn_operador.grid(row=1, column=1, sticky="w", padx=(16, 0))

        cards = tk.Frame(raiz, bg=FUNDO)
        cards.pack(fill="x")
        self.valores = {}
        for i, (chave, rotulo) in enumerate([("total", "Checklists"), ("aprovados", "✅ Aprovados"),
                                             ("reprovados", "❌ Reprovados"), ("taxa", "Taxa de aprovação")]):
            cards.columnconfigure(i, weight=1, uniform="cards")
            borda = tk.Frame(cards, bg=VERDE)
            borda.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 7, 0 if i == 3 else 7))
            card = tk.Frame(borda, bg="white", padx=self.px(14), pady=self.px(6), highlightbackground=BORDA, highlightthickness=1)
            card.pack(fill="both", expand=True, padx=(6, 0))
            tk.Label(card, text=rotulo, bg="white", fg=VERDE_ESCURO, font=self.fonte(10, "bold")).pack(anchor="w")
            self.valores[chave] = tk.Label(card, text="–", bg="white", fg=TEXTO, font=self.fonte(22))
            self.valores[chave].pack(anchor="w")

        self.lbl_ultimo = tk.Label(raiz, text="", bg=FUNDO, fg=TEXTO, font=self.fonte(10), anchor="w")
        self.lbl_ultimo.pack(fill="x", pady=(self.px(6), 0))

        rodape = tk.Frame(raiz, bg=FUNDO)
        rodape.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        tk.Frame(rodape, bg=BORDA, height=1).pack(fill="x", pady=(0, 6))
        self.lbl_conexao = tk.Label(rodape, text="⏳ Conectando ao GitHub…", bg=FUNDO, fg=SUAVE, font=self.fonte(9))
        self.lbl_conexao.pack(side="left")
        tk.Label(rodape, text="· GBMX", bg=FUNDO, fg=SUAVE, font=self.fonte(9)).pack(side="right")
        tk.Label(rodape, text="Desenvolvido por Engenharia de Automação", bg=FUNDO, fg=VERDE_ESCURO,
                 font=self.fonte(9, "bold")).pack(side="right")

        meio = tk.Frame(raiz, bg=FUNDO)
        meio.pack(fill="both", expand=True)
        meio.columnconfigure(0, weight=1)
        meio.rowconfigure(0, weight=2, uniform="m")
        meio.rowconfigure(1, weight=3, uniform="m")
        graficos = tk.Frame(meio, bg=FUNDO)
        graficos.grid(row=0, column=0, sticky="nsew")
        graficos.rowconfigure(0, weight=1)
        graficos.columnconfigure(0, weight=1, uniform="g")
        graficos.columnconfigure(1, weight=1, uniform="g")
        for col, titulo in enumerate(["Checklists por dia", "Itens marcados como NÃO OK"]):
            coluna = tk.Frame(graficos, bg=FUNDO)
            coluna.grid(row=0, column=col, sticky="nsew", padx=(0, 10) if col == 0 else (10, 0))
            self._secao(coluna, titulo).pack(anchor="w", pady=self.px(5))
            canvas = tk.Canvas(coluna, height=self.px(110), bg="white", highlightbackground=BORDA, highlightthickness=1)
            canvas.pack(fill="both", expand=True)
            if col == 0:
                self.cv_dias = canvas
                canvas.bind("<Configure>", lambda e: self._desenhar_dias())
            else:
                self.cv_itens = canvas
                canvas.bind("<Configure>", lambda e: self._desenhar_itens())

        baixo = tk.Frame(meio, bg=FUNDO)
        baixo.grid(row=1, column=0, sticky="nsew", pady=(self.px(2), 0))
        baixo.columnconfigure(0, weight=3, uniform="b")
        baixo.columnconfigure(1, weight=2, uniform="b")
        baixo.rowconfigure(1, weight=1)

        self._secao(baixo, "Histórico").grid(row=0, column=0, sticky="w", pady=self.px(5))
        hist = tk.Frame(baixo, bg=FUNDO)
        hist.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        colunas = [("data", "Data/Hora", 120), ("operador", "Operador", 260), ("matricula", "Matrícula", 80),
                   ("status", "Status", 120), ("nao_ok", "Itens NÃO OK", 90), ("pdf", "Arquivo PDF", 300)]
        self.tabela = ttk.Treeview(hist, columns=[c[0] for c in colunas], show="headings", selectmode="browse", height=3)
        for chave, titulo, largura in colunas:
            self.tabela.heading(chave, text=titulo, anchor="w")
            self.tabela.column(chave, width=self.px(largura), minwidth=self.px(50), anchor="e" if chave == "nao_ok" else "w",
                               stretch=True)
        self.tabela.tag_configure("REPROVADO", foreground=VERMELHO)
        self.tabela.tag_configure("APROVADO", foreground=VERDE_ESCURO)
        rolagem = ttk.Scrollbar(hist, orient="vertical", command=self.tabela.yview)
        self.tabela.configure(yscrollcommand=rolagem.set)
        self.tabela.pack(side="left", fill="both", expand=True)
        rolagem.pack(side="right", fill="y")
        self.tabela.bind("<<TreeviewSelect>>", self._ao_selecionar)

        topo_det = tk.Frame(baixo, bg=FUNDO)
        topo_det.grid(row=0, column=1, sticky="ew", pady=self.px(5), padx=(10, 0))
        self._secao(topo_det, "Respostas").pack(side="left")
        self.btn_pdf = tk.Button(topo_det, text="⬇ Salvar PDF", bg="#A9B5AD", fg="white", activebackground=VERDE_ESCURO,
                                 activeforeground="white", font=self.fonte(10, "bold"), relief="flat", padx=14,
                                 cursor="hand2", state="disabled", disabledforeground="white",
                                 command=self._salvar_pdf)
        self.btn_pdf.pack(side="right")
        det = tk.Frame(baixo, bg=FUNDO)
        det.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
        self.lbl_detalhe = tk.Label(det, text="Clique numa linha do histórico para ver as respostas.",
                                    bg=FUNDO, fg=SUAVE, font=self.fonte(10), anchor="w", justify="left", wraplength=self.px(560))
        self.lbl_detalhe.pack(fill="x", pady=(0, 4))
        self.tabela_det = ttk.Treeview(det, columns=("n", "item", "resposta"), show="headings", selectmode="none", height=3)
        for chave, titulo, largura in [("n", "Nº", 40), ("item", "Item", 260), ("resposta", "Resposta", 110)]:
            self.tabela_det.heading(chave, text=titulo, anchor="w")
            self.tabela_det.column(chave, width=self.px(largura), minwidth=self.px(40), stretch=chave == "item")
        self.tabela_det.tag_configure("NAO_OK", foreground=VERMELHO)
        self.tabela_det.tag_configure("OK", foreground=VERDE_ESCURO)
        self.tabela_det.pack(fill="both", expand=True)

        self.toast = tk.Label(self, bg=VERDE_ESCURO, fg="white", font=self.fonte(11, "bold"), padx=18, pady=12,
                              highlightbackground=LIMAO, highlightthickness=3)

    # ---------- dados ----------

    def _receber_dados(self, dados):
        self.checklists, self.respostas, self.erros = dados
        for erro in self.erros:
            print(f"[{agora()}] Arquivo ignorado — {erro}")
        ids = {c["id"] for c in self.checklists}
        if self.ids_conhecidos is not None:
            novos = [c for c in self.checklists if c["id"] not in self.ids_conhecidos]
            if novos:
                destaque = next((c for c in novos if c["status"] != "APROVADO"), novos[0])
                self._mostrar_alerta(destaque, len(novos) - 1)
        self.ids_conhecidos = ids
        self._atualizar_menu_operadores()
        self.atualizar()

    def _atualizar_menu_operadores(self):
        nomes = sorted({c["operador"] for c in self.checklists})
        self.operadores_vars = {n: self.operadores_vars.get(n) or tk.BooleanVar(value=False) for n in nomes}
        self.menu_operador.delete(0, "end")
        self.menu_operador.add_command(label="Todos os operadores", command=self._limpar_operadores)
        self.menu_operador.add_separator()
        for nome, var in self.operadores_vars.items():
            self.menu_operador.add_checkbutton(label=nome, variable=var, command=self.atualizar)

    def _limpar_operadores(self):
        for var in self.operadores_vars.values():
            var.set(False)
        self.atualizar()

    def _filtrar(self):
        dias = self.PERIODOS[self.periodo.get()]
        inicio = date.today() - timedelta(days=dias) if dias is not None else None
        escolhidos = {n for n, v in self.operadores_vars.items() if v.get()}
        return [c for c in self.checklists
                if (inicio is None or c["data_hora"].date() >= inicio)
                and (not escolhidos or c["operador"] in escolhidos)]

    def atualizar(self):
        escolhidos = [n for n, v in self.operadores_vars.items() if v.get()]
        if not escolhidos:
            self.btn_operador.config(text="Todos os operadores ▾")
        elif len(escolhidos) == 1:
            self.btn_operador.config(text=f"{escolhidos[0]} ▾")
        else:
            self.btn_operador.config(text=f"{len(escolhidos)} operadores selecionados ▾")

        lista = self._filtrar()
        self.filtrados = lista
        total = len(lista)
        aprovados = sum(c["status"] == "APROVADO" for c in lista)
        self.valores["total"].config(text=total)
        self.valores["aprovados"].config(text=aprovados)
        self.valores["reprovados"].config(text=total - aprovados)
        self.valores["taxa"].config(text=f"{aprovados / total:.0%}" if total else "—")

        if not self.checklists:
            self.lbl_ultimo.config(text="Nenhum checklist recebido ainda. Aguardando a sincronização com o GitHub…")
        elif not lista:
            self.lbl_ultimo.config(text="Nenhum checklist no período/filtro selecionado.")
        else:
            u = lista[0]
            self.lbl_ultimo.config(text=f"Último checklist:  {STATUS.get(u['status'], u['status'])} — {u['operador']} "
                                        f"(matrícula {u['matricula']}) em {u['data_hora']:%d/%m/%Y %H:%M}")

        por_dia = {}
        nao_ok = Counter()
        for c in lista:
            dia = por_dia.setdefault(c["data_hora"].date(), [0, 0])
            dia[0 if c["status"] == "APROVADO" else 1] += 1
            nao_ok.update(r["item"] for r in self.respostas.get(c["id"], []) if r["resposta"] != "OK")
        self.dados_dias = sorted(por_dia.items())
        self.dados_itens = sorted(((ROTULOS_ITENS[i], nao_ok[i]) for i in ROTULOS_ITENS), key=lambda x: -x[1])
        self._desenhar_dias()
        self._desenhar_itens()

        self.tabela.delete(*self.tabela.get_children())
        for c in lista:
            self.tabela.insert("", "end", iid=c["id"], tags=(c["status"],), values=(
                f"{c['data_hora']:%d/%m/%Y %H:%M}", c["operador"], c["matricula"],
                STATUS.get(c["status"], c["status"]), c["itens_nao_ok"], c["pdf"]))
        if self.selecionado and self.tabela.exists(self.selecionado):
            self.tabela.selection_set(self.selecionado)
        else:
            self.selecionado = None
            self._mostrar_detalhe()

    # ---------- gráficos ----------

    @staticmethod
    def _passo(maximo):
        bruto = max(1, maximo) / 4
        base = 10 ** len(str(int(bruto))) // 10 or 1
        return max(1, next(m * base for m in (1, 2, 5, 10) if m * base >= bruto))

    def _desenhar_dias(self):
        cv = self.cv_dias
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 50:
            return
        px = self.px
        m_esq, m_dir, m_topo, m_base = px(40), px(14), px(34), px(26)
        yl = px(15)
        for x0, cor, texto in ((w / 2 - px(150), VERDE, "✅ APROVADO"), (w / 2 + px(20), VERMELHO, "❌ REPROVADO")):
            cv.create_rectangle(x0, yl - px(6), x0 + px(12), yl + px(6), fill=cor, outline="")
            cv.create_text(x0 + px(18), yl, text=texto, anchor="w", font=self.fonte(9), fill=TEXTO)
        if not self.dados_dias:
            cv.create_text(w / 2, h / 2, text="Sem dados no período", fill=SUAVE, font=self.fonte(10))
            return
        maximo = max(a + r for _, (a, r) in self.dados_dias)
        passo = self._passo(maximo)
        topo = -(-maximo // passo) * passo
        y = lambda v: h - m_base - v / topo * (h - m_topo - m_base)
        for v in range(0, topo + 1, passo):
            cv.create_line(m_esq, y(v), w - m_dir, y(v), fill="#EEF1EE")
            cv.create_text(m_esq - 6, y(v), text=v, anchor="e", fill=SUAVE, font=self.fonte(8))
        faixa = (w - m_esq - m_dir) / len(self.dados_dias)
        larg = min(px(46), faixa * 0.7)
        cada = max(1, int(len(self.dados_dias) / max(1, (w - m_esq - m_dir) // px(46))) + 1)
        for i, (dia, (apr, rep)) in enumerate(self.dados_dias):
            x = m_esq + i * faixa + (faixa - larg) / 2
            if apr:
                cv.create_rectangle(x, y(apr), x + larg, y(0), fill=VERDE, outline="")
            if rep:
                cv.create_rectangle(x, y(apr + rep), x + larg, y(apr) - (1 if apr else 0), fill=VERMELHO, outline="")
            cv.create_text(x + larg / 2, y(apr + rep) - px(8), text=apr + rep, fill=TEXTO, font=self.fonte(8, "bold"))
            if i % cada == 0:
                cv.create_text(x + larg / 2, h - m_base + px(12), text=f"{dia:%d/%m}", fill=SUAVE, font=self.fonte(8))

    def _desenhar_itens(self):
        cv = self.cv_itens
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 50 or not self.dados_itens:
            return
        px = self.px
        m_esq, m_dir, m_v = px(230), px(30), px(8)
        maximo = max(1, max(q for _, q in self.dados_itens))
        faixa = (h - 2 * m_v) / len(self.dados_itens)
        for i, (rotulo, qtd) in enumerate(self.dados_itens):
            yc = m_v + i * faixa + faixa / 2
            larg = qtd / maximo * (w - m_esq - m_dir)
            cv.create_text(m_esq - px(8), yc, text=rotulo, anchor="e", fill=TEXTO, font=self.fonte(9))
            if larg:
                cv.create_rectangle(m_esq, yc - px(6), m_esq + larg, yc + px(6), fill=VERMELHO, outline="")
            cv.create_text(m_esq + larg + px(5), yc, text=qtd, anchor="w", fill=SUAVE, font=self.fonte(9))

    # ---------- detalhe / PDF ----------

    def _ao_selecionar(self, _evento):
        sel = self.tabela.selection()
        if sel:
            self.selecionado = sel[0]
            self._mostrar_detalhe()

    def _mostrar_detalhe(self):
        self.tabela_det.delete(*self.tabela_det.get_children())
        c = next((c for c in self.checklists if c["id"] == self.selecionado), None)
        if c is None:
            self.lbl_detalhe.config(text="Clique numa linha do histórico para ver as respostas.", fg=SUAVE)
            self.btn_pdf.config(state="disabled", bg="#A9B5AD")
            return
        self.lbl_detalhe.config(text=f"{c['operador']} em {c['data_hora']:%d/%m/%Y %H:%M} — "
                                     f"{STATUS.get(c['status'], c['status'])}", fg=TEXTO)
        for r in self.respostas.get(c["id"], []):
            ok = r["resposta"] == "OK"
            self.tabela_det.insert("", "end", tags=("OK" if ok else "NAO_OK",),
                                   values=(r["item"], r["rotulo"], "✅ OK" if ok else "❌ NÃO OK"))
        tem_pdf = (self.sinc.pasta / c["pdf"]).is_file()
        self.btn_pdf.config(state="normal" if tem_pdf else "disabled", bg=VERDE if tem_pdf else "#A9B5AD")

    def _salvar_pdf(self):
        c = next((c for c in self.checklists if c["id"] == self.selecionado), None)
        if c is None:
            return
        destino = filedialog.asksaveasfilename(parent=self, initialfile=c["pdf"], defaultextension=".pdf",
                                               filetypes=[("Arquivo PDF", "*.pdf")])
        if not destino:
            return
        try:
            shutil.copyfile(self.sinc.pasta / c["pdf"], destino)
            self._mostrar_toast(f"✅ PDF salvo em {destino}")
        except OSError as e:
            self._mostrar_toast(f"❌ Não foi possível salvar: {e}")

    # ---------- status / avisos ----------

    def _mostrar_estado(self, online, erro, hora):
        self.lbl_legenda.config(text=f"Atualização automática a cada {INTERVALO_SEGUNDOS} s · última verificação às {hora}")
        if online:
            commit = self.sinc.estado.get("commit", "")[:7]
            self.lbl_conexao.config(text=f"🟢 Online · sincronizado com o GitHub (commit {commit})", fg=SUAVE)
            self.lbl_offline.pack_forget()
        else:
            self.lbl_conexao.config(text=f"⚠️ Offline — exibindo os últimos dados baixados · {erro}", fg=VERMELHO)
            self.lbl_offline.config(text=f"⚠️ {erro} Exibindo os últimos dados baixados.")
            self.lbl_offline.pack(fill="x", pady=(4, 0), before=self.ancora_avisos)

    def _mostrar_alerta(self, c, outros=0):
        self._fechar_alerta()
        reprovado = c["status"] != "APROVADO"
        cor = "#B3261E" if reprovado else VERDE_ESCURO
        cor_borda = "#6E0F0A" if reprovado else LIMAO
        claro = "#FFDAD6" if reprovado else "#DDEFD9"
        px, f = self.px, self.fonte

        self.alerta = tk.Frame(self, bg=cor_borda)
        self.alerta.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.62)
        caixa = tk.Frame(self.alerta, bg=cor, padx=px(36), pady=px(26))
        caixa.pack(fill="both", expand=True, padx=px(8), pady=px(8))

        lado = px(130)
        icone = tk.Canvas(caixa, width=lado, height=lado, bg=cor, highlightthickness=0)
        icone.pack(pady=(0, px(8)))
        icone.create_oval(px(4), px(4), lado - px(4), lado - px(4), fill="white", outline="")
        traco = dict(fill=cor, width=px(14), capstyle="round", joinstyle="round")
        if reprovado:
            a, b = lado * 0.32, lado * 0.68
            icone.create_line(a, a, b, b, **traco)
            icone.create_line(a, b, b, a, **traco)
        else:
            icone.create_line(lado * 0.28, lado * 0.52, lado * 0.44, lado * 0.68, lado * 0.73, lado * 0.36, **traco)

        tk.Label(caixa, text="CHECKLIST REPROVADO" if reprovado else "CHECKLIST APROVADO",
                 bg=cor, fg="white", font=f(34, "bold")).pack()
        tk.Label(caixa, text=f"Novo checklist recebido · {c['data_hora']:%d/%m/%Y às %H:%M}",
                 bg=cor, fg=claro, font=f(13)).pack(pady=(0, px(14)))
        tk.Label(caixa, text=c["operador"], bg=cor, fg="white", font=f(24, "bold"),
                 wraplength=px(900)).pack()
        tk.Label(caixa, text=f"Matrícula {c['matricula']}", bg=cor, fg=claro, font=f(14)).pack()

        if reprovado:
            itens = [r["rotulo"] for r in self.respostas.get(c["id"], []) if r["resposta"] != "OK"]
            lista = tk.Frame(caixa, bg="white", padx=px(20), pady=px(12))
            lista.pack(fill="x", pady=(px(18), 0))
            colunas = 2 if len(itens) <= 6 else 3
            tk.Label(lista, text=f"{len(itens)} item(ns) marcado(s) como NÃO OK:", bg="white", fg=cor,
                     font=f(13, "bold")).grid(row=0, column=0, columnspan=colunas, sticky="w", pady=(0, px(6)))
            for i, rotulo in enumerate(itens):
                tk.Label(lista, text=f"✕  {rotulo}", bg="white", fg=TEXTO, font=f(14, "bold"), anchor="w"
                         ).grid(row=1 + i // colunas, column=i % colunas, sticky="w", padx=(0, px(30)), pady=px(2))
        if outros:
            tk.Label(caixa, text=f"+ {outros} outro(s) checklist(s) recebido(s) — veja o histórico",
                     bg=cor, fg=claro, font=f(11)).pack(pady=(px(12), 0))

        tk.Button(caixa, text="CIENTE", bg="white", fg=cor, activebackground=claro, activeforeground=cor,
                  font=f(16, "bold"), relief="flat", padx=px(40), pady=px(6), cursor="hand2",
                  command=self._fechar_alerta).pack(pady=(px(20), 0))
        self.lbl_contagem = tk.Label(caixa, text="", bg=cor, fg=claro, font=f(10))
        self.lbl_contagem.pack(pady=(px(6), 0))

        self.alerta.lift()
        self.bind("<Escape>", lambda e: self._fechar_alerta())
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(800, lambda: self.attributes("-topmost", False))
        if reprovado:
            self.bell()
        self._contagem_alerta(15)

    def _contagem_alerta(self, segundos):
        if not getattr(self, "alerta", None):
            return
        if segundos <= 0:
            self._fechar_alerta()
            return
        self.lbl_contagem.config(text=f"Fecha automaticamente em {segundos} s")
        self._alerta_id = self.after(1000, self._contagem_alerta, segundos - 1)

    def _fechar_alerta(self):
        if getattr(self, "_alerta_id", None):
            self.after_cancel(self._alerta_id)
            self._alerta_id = None
        if getattr(self, "alerta", None):
            self.alerta.destroy()
            self.alerta = None

    def _mostrar_toast(self, texto):
        self.toast.config(text=texto)
        self.toast.place(relx=1.0, rely=1.0, x=-24, y=-40, anchor="se")
        self.toast.lift()
        if getattr(self, "_toast_id", None):
            self.after_cancel(self._toast_id)
        self._toast_id = self.after(6000, self.toast.place_forget)

    def _processar_fila(self):
        try:
            while True:
                msg = self.fila.get_nowait()
                if msg[0] == "dados":
                    self._receber_dados(msg[1])
                elif msg[0] == "estado":
                    self._mostrar_estado(*msg[1:])
        except queue.Empty:
            pass
        self.after(200, self._processar_fila)

    def _ao_fechar(self):
        self.parar.set()
        self.destroy()


# ============================================================
#  MAIN
# ============================================================

def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    print("=== Painel de Checklists GBMX ===")
    print(f"Repositório: {REPOSITORIO} ({BRANCH})")
    print(f"Dados locais: {PASTA_DADOS}")

    sinc = Sincronizador()
    fila = queue.Queue()
    parar = threading.Event()
    janela = PainelChecklists(sinc, fila, parar)
    threading.Thread(target=loop_sincronizacao, args=(sinc, fila, parar), daemon=True).start()
    janela.mainloop()
    parar.set()


if __name__ == "__main__":
    main()
