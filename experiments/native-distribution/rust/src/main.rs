//! EXPERIMENTAL, BENCHMARK-ONLY. Gate X packages this drive as a native
//! binary to measure distribution and startup. It is not the required check
//! and not a production port.
//!
//! A Rust port of the hidden Unicode check of agent-guardrails 40382e2:
//! hidden.py (scan, mixed_script_words, hidden_text, visible, snippet,
//! describe), rules.py (Budget, _unicode_line, check_unicode,
//! check_changed_file), report.py (Reporter) and the decoding half of
//! gitdata._classify. hidden.py is no_ai_marks/chars.py at 747c07a, MIT; see
//! ../NOTICE.md. It reads an AGCAP1 capture (bench/capture.py). Git, the
//! event, the trailer, identity and attribution checks are not ported, and
//! there is no process supervisor.
//!
//!     agent-guardrails drive <capture>
//!     agent-guardrails --unidata
//!
//! Derived from the Gate X benchmark prototype: the kernel and parity modes
//! are gone, every report line is written and flushed on its own as
//! report.Reporter does, a malformed or oversized capture is a controlled
//! failure, and a panic ends in one sanitized line and exit status 1.

mod tables;

use std::collections::HashSet;
use std::fmt::Write as _;
use std::io::{Read as _, Write as _};
use std::process::ExitCode;

const INVISIBLE: u16 = 1 << 0;
const CONTROL: u16 = 1 << 1;
const USPACE: u16 = 1 << 2;
const PRIVATE: u16 = 1 << 3;
const ESCAPE: u16 = 1 << 4;
const RTL: u16 = 1 << 5;
const DIGIT: u16 = 1 << 6;
const WORD: u16 = 1 << 7;
const ALPHA: u16 = 1 << 8;
const PRINTABLE: u16 = 1 << 9;
const LOOKALIKE: u16 = 1 << 10;
const DOMAIN: u16 = 1 << 11;
const SCRIPT_SHIFT: u16 = 12;

const S_LATIN: u16 = 1;
const S_GREEK: u16 = 2;
const S_CJK: u16 = 3;
const S_CJK_OTHER: u16 = 4;
const S_MONGOLIAN: u16 = 5;
const S_JOINING: u16 = 6;

const ZWJ: u32 = 0x200D;
const ZWNJ: u32 = 0x200C;
const BOM: u32 = 0xFEFF;
const BLACK_FLAG: u32 = 0x1F3F4;
const CANCEL_TAG: u32 = 0xE007F;
const IDEOGRAPHIC_SPACE: u32 = 0x3000;
const POP_ISOLATE: u32 = 0x2069;

const QUOTE_LIMIT: usize = 200;
const BINARY_EXTENSIONS: [&str; 17] = [
    "png", "jpg", "jpeg", "gif", "webp", "ico", "pdf", "zip", "gz", "woff", "woff2", "ttf", "otf",
    "mp4", "mov", "mp3", "wav",
];

/// The largest capture the drive reads. The biggest Gate X corpus is about
/// 14 MB; anything past this bound is refused before it is held in memory.
const MAX_CAPTURE_BYTES: u64 = 64 * 1024 * 1024;

#[inline(always)]
fn props(cp: u32) -> u16 {
    let shift = tables::SHIFT;
    let block = tables::STAGE1[(cp >> shift) as usize] as usize;
    let index = tables::STAGE2[(block << shift) | (cp as usize & ((1usize << shift) - 1))];
    tables::PROPS[index as usize]
}

#[inline(always)]
fn script(cp: u32) -> u16 {
    props(cp) >> SCRIPT_SHIFT
}

#[inline(always)]
fn joining(s: u16) -> bool {
    s == S_MONGOLIAN || s == S_JOINING
}

#[inline(always)]
fn is_vs(cp: u32) -> bool {
    (0xFE00..=0xFE0F).contains(&cp) || (0xE0100..=0xE01EF).contains(&cp)
}

#[inline(always)]
fn is_tag(cp: u32) -> bool {
    (0xE0000..=0xE007F).contains(&cp)
}

#[inline(always)]
fn is_bidi_mark(cp: u32) -> bool {
    cp == 0x061C || cp == 0x200E || cp == 0x200F
}

#[inline(always)]
fn allowed_control(cp: u32) -> bool {
    matches!(cp, 0x09 | 0x0A | 0x0C | 0x0D)
}

fn emojiish(cp: u32) -> bool {
    (0x1F000..=0x1FAFF).contains(&cp)
        || (0x2190..=0x2BFF).contains(&cp)
        || matches!(
            cp,
            0x00A9 | 0x00AE | 0x203C | 0x2049 | 0x2122 | 0x2139 | 0x3030 | 0x303D | 0x3297 | 0x3299
        )
}

fn non_joining_arabic(cp: u32) -> bool {
    matches!(
        cp,
        0x0622 | 0x0623 | 0x0624 | 0x0625 | 0x0627 | 0x062F | 0x0630 | 0x0631 | 0x0632 | 0x0648 | 0x0698
    )
}

fn latin_lookalike(cp: u32) -> bool {
    matches!(
        cp,
        0x0251 | 0x0261 | 0x026A | 0x1D04 | 0x1D0F | 0x1D1C | 0x1D20 | 0x1D21 | 0x1D22 | 0xA731
    )
}

fn fake_latin(cp: u32) -> bool {
    (0xFF21..=0xFF3A).contains(&cp)
        || (0xFF41..=0xFF5A).contains(&cp)
        || (0x1D400..=0x1D6A3).contains(&cp)
        || cp == 0x212A
        || (0x2160..=0x217F).contains(&cp)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Rule {
    Invisible,
    Space,
    Private,
}

impl Rule {
    fn title(self) -> &'static str {
        match self {
            Rule::Invisible => "Invisible character",
            Rule::Space => "Unusual space",
            Rule::Private => "Private-use character",
        }
    }
    fn noun(self) -> &'static str {
        match self {
            Rule::Invisible => "invisible character",
            Rule::Space => "unusual space",
            Rule::Private => "private-use character",
        }
    }
}

fn balanced_isolates(cps: &[u32]) -> bool {
    let mut depth: i64 = 0;
    for &cp in cps {
        if (0x2066..=0x2068).contains(&cp) {
            depth += 1;
        } else if cp == POP_ISOLATE {
            depth -= 1;
            if depth < 0 {
                return false;
            }
        }
    }
    depth == 0
}

fn joiner_ok(cps: &[u32], i: usize) -> bool {
    let mut j = i as isize - 1;
    while j >= 0 && (is_vs(cps[j as usize]) || (0x1F3FB..=0x1F3FF).contains(&cps[j as usize])) {
        j -= 1;
    }
    let before = if j >= 0 { Some(cps[j as usize]) } else { None };
    let after = cps.get(i + 1).copied();
    if cps[i] == ZWJ {
        if let (Some(b), Some(a)) = (before, after) {
            if emojiish(b) && emojiish(a) {
                return true;
            }
        }
    }
    let prev = if i > 0 { Some(cps[i - 1]) } else { None };
    if matches!(prev, Some(ZWJ) | Some(ZWNJ)) || (cps[i] == ZWNJ && prev.is_some_and(non_joining_arabic)) {
        return false;
    }
    match prev {
        None => false,
        Some(p) => {
            joining(script(p))
                && match after {
                    None => true,
                    Some(a) => a == 0x20 || joining(script(a)),
                }
        }
    }
}

fn variation_ok(cps: &[u32], i: usize) -> bool {
    if i == 0 {
        return false;
    }
    let (base, cp) = (cps[i - 1], cps[i]);
    if cp >= 0xE0100 {
        return script(base) == S_CJK;
    }
    if base < 0x80 {
        return b"0123456789#*".contains(&(base as u8));
    }
    script(base) != S_LATIN
}

fn flag_ok(cps: &[u32], start: usize, end: usize) -> bool {
    start > 0
        && cps[start - 1] == BLACK_FLAG
        && end - start >= 2
        && cps[end - 1] == CANCEL_TAG
        && cps[start..end - 1].iter().all(|&c| (0xE0020..=0xE007E).contains(&c))
}

fn rtl_of(cps: &[u32]) -> bool {
    cps.iter().any(|&c| !is_bidi_mark(c) && props(c) & RTL != 0)
}

/// hidden.scan. emit returns false to stop, as the caller's limit does.
fn scan<F: FnMut(Rule, usize, u32) -> bool>(cps: &[u32], at_file_start: bool, emit: &mut F) {
    let n = cps.len();
    let mut rtl: Option<bool> = None;
    let mut i = 0;
    macro_rules! hit {
        ($rule:expr, $col:expr, $cp:expr) => {
            if !emit($rule, $col, $cp) {
                return;
            }
        };
    }
    while i < n {
        let cp = cps[i];
        if cp < 0x80 {
            if (cp < 0x20 || cp == 0x7F) && !allowed_control(cp) {
                hit!(Rule::Invisible, i + 1, cp);
            }
            i += 1;
            continue;
        }
        let tag = is_tag(cp);
        if tag || is_vs(cp) {
            let mut j = i;
            while j < n && (if tag { is_tag(cps[j]) } else { is_vs(cps[j]) }) {
                j += 1;
            }
            let ok = if tag { flag_ok(cps, i, j) } else { j - i == 1 && variation_ok(cps, i) };
            if !ok {
                for k in i..j {
                    hit!(Rule::Invisible, k + 1, cps[k]);
                }
            }
            i = j;
            continue;
        }
        if cp == BOM && i == 0 && at_file_start {
        } else if cp == ZWJ || cp == ZWNJ {
            if !joiner_ok(cps, i) {
                hit!(Rule::Invisible, i + 1, cp);
            }
        } else if is_bidi_mark(cp) {
            if !*rtl.get_or_insert_with(|| rtl_of(cps)) {
                hit!(Rule::Invisible, i + 1, cp);
            }
        } else if (0x2066..=0x2069).contains(&cp) {
            if !(*rtl.get_or_insert_with(|| rtl_of(cps)) && balanced_isolates(cps)) {
                hit!(Rule::Invisible, i + 1, cp);
            }
        } else if (0x180B..=0x180F).contains(&cp) {
            if !(i > 0 && script(cps[i - 1]) == S_MONGOLIAN) {
                hit!(Rule::Invisible, i + 1, cp);
            }
        } else {
            let p = props(cp);
            if p & (INVISIBLE | CONTROL) != 0 {
                hit!(Rule::Invisible, i + 1, cp);
            } else if p & USPACE != 0 {
                let cjk = cp == IDEOGRAPHIC_SPACE
                    && cps.iter().any(|&c| {
                        c != IDEOGRAPHIC_SPACE && {
                            let s = script(c);
                            s == S_CJK || s == S_CJK_OTHER
                        }
                    });
                let in_number = matches!(cp, 0x00A0 | 0x2007 | 0x2009 | 0x202F)
                    && 0 < i
                    && i + 1 < n
                    && props(cps[i - 1]) & DIGIT != 0
                    && (props(cps[i + 1]) & DIGIT != 0 || cps[i + 1] == 0x25);
                if !(cjk || in_number) {
                    hit!(Rule::Space, i + 1, cp);
                }
            } else if p & PRIVATE != 0 {
                hit!(Rule::Private, i + 1, cp);
            }
        }
        i += 1;
    }
}

struct Word {
    column: usize,
    start: usize,
    end: usize,
    odd: Vec<u32>,
}

/// hidden.mixed_script_words, with _WORD.finditer as maximal runs of the
/// generated [^\W\d_] class.
fn mixed_script_words(cps: &[u32]) -> Vec<Word> {
    let mut out = Vec::new();
    let mut latin_line: Option<(bool, bool)> = None;
    let n = cps.len();
    let mut i = 0;
    while i < n {
        if props(cps[i]) & WORD == 0 {
            i += 1;
            continue;
        }
        let start = i;
        while i < n && props(cps[i]) & WORD != 0 {
            i += 1;
        }
        let word = &cps[start..i];
        if word.iter().all(|&c| c < 0x80) {
            continue;
        }
        let odd: Vec<u32> = if word.iter().any(|&c| c < 0x80 || script(c) == S_LATIN) {
            let mut odd: Vec<u32> =
                word.iter().copied().filter(|&c| props(c) & LOOKALIKE != 0 || fake_latin(c)).collect();
            if odd.is_empty() && word.iter().all(|&c| c < 0x80 || latin_lookalike(c)) {
                odd = word.iter().copied().filter(|&c| latin_lookalike(c)).collect();
            }
            odd
        } else {
            let (latin, foreign) = *latin_line.get_or_insert_with(|| {
                (
                    cps.iter().any(|&c| c < 0x80 && props(c) & ALPHA != 0),
                    cps.iter().any(|&c| {
                        let p = props(c);
                        p & ALPHA != 0 && c >= 0x80 && p & LOOKALIKE == 0 && (p >> SCRIPT_SHIFT) != S_LATIN
                    }),
                )
            });
            let whole = latin
                && !foreign
                && word.iter().all(|&c| props(c) & LOOKALIKE != 0 && script(c) != S_GREEK);
            if whole { word.to_vec() } else { Vec::new() }
        };
        if !odd.is_empty() {
            out.push(Word { column: start + 1, start, end: i, odd });
        }
    }
    out
}

fn string_of(cps: &[u32]) -> String {
    cps.iter().map(|&c| char::from_u32(c).expect("scalar")).collect()
}

fn py_isprintable(text: &str) -> bool {
    text.chars().all(|c| props(c as u32) & PRINTABLE != 0)
}

fn hidden_text(cps: &[u32]) -> String {
    let mut parts: Vec<String> = Vec::new();
    let bits: Vec<u8> =
        cps.iter().filter(|&&c| c == 0x200B || c == 0x200C).map(|&c| u8::from(c != 0x200B)).collect();
    if bits.len() >= 8 {
        let data: Vec<u8> = bits.chunks_exact(8).map(|b| b.iter().fold(0u8, |a, &bit| (a << 1) | bit)).collect();
        let decoded = String::from_utf8_lossy(&data);
        if decoded.is_ascii()
            && py_isprintable(&decoded)
            && decoded.chars().any(|c| !matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r' | '\x1c'..='\x1f'))
        {
            parts.push(decoded.into_owned());
        }
    }
    let tags: String = cps
        .iter()
        .filter(|&&c| (0xE0020..=0xE007E).contains(&c))
        .map(|&c| char::from_u32(c - 0xE0000).expect("ascii"))
        .collect();
    if tags.len() > 1 {
        parts.push(tags);
    }
    let selectors: Vec<u8> = cps
        .iter()
        .filter(|&&c| is_vs(c))
        .map(|&c| if c <= 0xFE0F { (c - 0xFE00) as u8 } else { (c - 0xE0100 + 16) as u8 })
        .collect();
    if selectors.len() > 1 {
        let decoded = String::from_utf8_lossy(&selectors);
        if py_isprintable(&decoded) {
            parts.push(decoded.into_owned());
        }
    }
    parts.join(" ")
}

fn push_visible(out: &mut String, cp: u32) {
    if cp == 0x09 || props(cp) & ESCAPE == 0 {
        out.push(char::from_u32(cp).expect("scalar"));
    } else {
        let _ = write!(out, "<U+{cp:04X}>");
    }
}

fn visible_cps(cps: &[u32]) -> String {
    let mut out = String::with_capacity(cps.len());
    for &cp in cps {
        push_visible(&mut out, cp);
    }
    out
}

fn visible_str(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        push_visible(&mut out, c as u32);
    }
    out
}

fn snippet(cps: &[u32], column: usize) -> String {
    let width = 120;
    let column = if column == 0 { 1 } else { column };
    let n = cps.len();
    let start = column.saturating_sub(41);
    let piece = &cps[start.min(n)..(start + width).min(n)];
    let shown = visible_cps(piece);
    let mut text = String::new();
    if start > 0 {
        text.push_str("...");
    }
    text.push_str(shown.trim_matches(|c| c == ' ' || c == '\t'));
    if start + width < n {
        text.push_str("...");
    }
    text
}

fn describe(cp: u32) -> String {
    if props(cp) & DOMAIN == 0 {
        panic!("describe() asked about U+{cp:04X}, outside the generated name table");
    }
    match tables::NAMES.binary_search_by_key(&cp, |&(c, _)| c) {
        Ok(index) => format!("U+{cp:04X} {}", tables::NAMES[index].1),
        Err(_) => format!("U+{cp:04X}"),
    }
}

fn names<I: Iterator<Item = u32>>(cps: I) -> String {
    let mut seen: HashSet<String> = HashSet::new();
    let mut order: Vec<String> = Vec::new();
    for cp in cps {
        let name = describe(cp);
        if !seen.contains(&name) {
            seen.insert(name.clone());
            order.push(name);
        }
    }
    let mut text = order.iter().take(3).cloned().collect::<Vec<_>>().join(", ");
    if order.len() > 3 {
        text.push_str(", ...");
    }
    text
}

fn quote(text: &str) -> String {
    match text.char_indices().nth(QUOTE_LIMIT) {
        None => text.to_string(),
        Some((cut, _)) => format!("{}...", &text[..cut]),
    }
}

fn commas(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (index, c) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

#[derive(Clone, Copy)]
struct Limits {
    work: u64,
    findings: u64,
    line_length: u64,
    hit: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { work: 100_000_000, findings: 1000, line_length: 4_000_000, hit: 100_000 }
    }
}

#[derive(Default)]
struct Budget {
    work: u64,
    findings: u64,
}

struct Finding {
    title: &'static str,
    message: String,
    severity: &'static str,
    file: Option<String>,
    line: Option<u64>,
}

struct LimitReached {
    message: String,
    findings: Vec<Finding>,
}

impl LimitReached {
    fn new(message: String) -> Self {
        LimitReached { message, findings: Vec::new() }
    }
    fn after(mut self, mut earlier: Vec<Finding>) -> Self {
        earlier.append(&mut self.findings);
        self.findings = earlier;
        self
    }
}

impl Budget {
    fn found(&mut self, limits: &Limits) -> Result<(), LimitReached> {
        self.findings += 1;
        if self.findings > limits.findings {
            return Err(LimitReached::new(format!(
                "not fully checked: stopped after {} findings, the most this action reports for one pull request",
                limits.findings
            )));
        }
        Ok(())
    }
}

struct Ctx<'a> {
    limits: &'a Limits,
    warn: bool,
}

#[allow(clippy::too_many_arguments)]
fn unicode_line(
    ctx: &Ctx,
    line: &str,
    place: &str,
    budget: &mut Budget,
    at_file_start: bool,
    file: Option<&str>,
    number: Option<u64>,
) -> Result<Vec<Finding>, LimitReached> {
    let limits = ctx.limits;
    // Budget.spend: the length is judged before the line is copied.
    let length = line.chars().count() as u64;
    if length > limits.line_length {
        return Err(LimitReached::new(format!(
            "not fully checked: the hidden Unicode check stopped at {place}, longer than the {} characters this action scans in one line",
            commas(limits.line_length)
        )));
    }
    let cps: Vec<u32> = line.chars().map(|c| c as u32).collect();
    let searches = cps.iter().filter(|&&c| matches!(c, 0x2066..=0x2069 | 0x3000)).count() as u64;
    budget.work += length * (1 + searches);
    if budget.work > limits.work {
        return Err(LimitReached::new(format!(
            "not fully checked: the hidden Unicode check stopped at {place}, past the {} steps this action allows for one pull request",
            commas(limits.work)
        )));
    }

    let mut hits: Vec<(Rule, Vec<(usize, u32)>)> = Vec::new();
    let mut seen: u64 = 0;
    let mut over = false;
    scan(&cps, at_file_start, &mut |rule, column, cp| {
        seen += 1;
        if seen > limits.hit {
            over = true;
            return false;
        }
        match hits.iter_mut().find(|(r, _)| *r == rule) {
            Some((_, found)) => found.push((column, cp)),
            None => hits.push((rule, vec![(column, cp)])),
        }
        true
    });
    if over {
        return Err(LimitReached::new(format!(
            "not fully checked: the hidden Unicode check stopped at {place}, which has more than the {} suspicious characters this action collects in one line",
            commas(limits.hit)
        )));
    }

    let mut findings: Vec<Finding> = Vec::new();
    let mut place_upper = String::with_capacity(place.len());
    let mut chars = place.chars();
    if let Some(first) = chars.next() {
        place_upper.extend(first.to_uppercase());
        place_upper.push_str(chars.as_str());
    }
    let finding = |budget: &mut Budget, title: &'static str, warning_rule: bool, message: String, column: usize| {
        budget.found(limits)?;
        let severity = if ctx.warn || warning_rule { "warning" } else { "error" };
        let text = format!("{place_upper} {message}; the line reads: {}", snippet(&cps, column));
        Ok::<Finding, LimitReached>(Finding {
            title,
            message: text,
            severity,
            file: file.map(str::to_string),
            line: number,
        })
    };
    for (rule, found) in &hits {
        let count = found.len();
        let mut message = format!(
            "has {count} {}{}: {}",
            rule.noun(),
            if count == 1 { "" } else { "s" },
            names(found.iter().map(|&(_, cp)| cp))
        );
        if *rule == Rule::Invisible {
            let invisible: Vec<u32> = found.iter().map(|&(_, cp)| cp).collect();
            let payload = hidden_text(&invisible);
            if !payload.is_empty() {
                let _ = write!(message, " (hidden text: '{}')", quote(&payload));
            }
        }
        match finding(budget, rule.title(), *rule == Rule::Space, message, found[0].0) {
            Ok(made) => findings.push(made),
            Err(error) => return Err(error.after(findings)),
        }
    }
    for word in mixed_script_words(&cps) {
        let text = string_of(&cps[word.start..word.end]);
        let message = format!(
            "has the word '{}', which mixes Latin with {}",
            quote(&text),
            names(word.odd.iter().copied())
        );
        match finding(budget, "Look-alike letter", false, message, word.column) {
            Ok(made) => findings.push(made),
            Err(error) => return Err(error.after(findings)),
        }
    }
    Ok(findings)
}

fn check_unicode(ctx: &Ctx, text: &str, place: &str, budget: &mut Budget) -> Result<Vec<Finding>, LimitReached> {
    let lines: Vec<&str> = text.split('\n').collect();
    let mut findings = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        let label = if lines.len() == 1 { place.to_string() } else { format!("line {} of {place}", index + 1) };
        match unicode_line(ctx, line.trim_end_matches('\r'), &label, budget, false, None, None) {
            Ok(mut found) => findings.append(&mut found),
            Err(error) => return Err(error.after(findings)),
        }
    }
    Ok(findings)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Text,
    AllowedBinary,
    RejectedBinary,
    Undecodable,
    Submodule,
}

struct Changed {
    path: String,
    kind: Kind,
    added: Vec<(u64, String)>,
}

fn check_changed_file(ctx: &Ctx, changed: &Changed, budget: &mut Budget) -> Result<Vec<Finding>, LimitReached> {
    match changed.kind {
        Kind::RejectedBinary => {
            budget.found(ctx.limits)?;
            let mut formats: Vec<&str> = BINARY_EXTENSIONS.to_vec();
            formats.sort_unstable();
            Ok(vec![Finding {
                title: "Cannot scan a changed file",
                message: format!(
                    "cannot scan {}: binary content. Git treats the file as binary, so its lines cannot be checked. Only these formats may be binary: {}.",
                    changed.path,
                    formats.join(", ")
                ),
                severity: "error",
                file: Some(changed.path.clone()),
                line: None,
            }])
        }
        Kind::Undecodable => {
            budget.found(ctx.limits)?;
            Ok(vec![Finding {
                title: "Cannot scan a changed file",
                message: format!("cannot decode {}: its name or its content is not valid UTF-8.", changed.path),
                severity: "error",
                file: Some(changed.path.clone()),
                line: None,
            }])
        }
        Kind::AllowedBinary | Kind::Submodule => Ok(Vec::new()),
        Kind::Text => {
            let mut findings = Vec::new();
            for (number, line) in &changed.added {
                let place = format!("line {number} of {}", changed.path);
                match unicode_line(ctx, line, &place, budget, *number == 1, Some(&changed.path), Some(*number)) {
                    Ok(mut found) => findings.append(&mut found),
                    Err(error) => return Err(error.after(findings)),
                }
            }
            Ok(findings)
        }
    }
}

/// posixpath.splitext(path)[1][1:]
fn extension(path: &str) -> &str {
    let bytes = path.as_bytes();
    let sep = path.rfind('/').map_or(-1, |i| i as isize);
    if let Some(dot) = path.rfind('.') {
        if dot as isize > sep {
            let mut index = (sep + 1) as usize;
            while index < dot {
                if bytes[index] != b'.' {
                    return &path[dot + 1..];
                }
                index += 1;
            }
        }
    }
    ""
}

struct CapFile {
    kind: String,
    path: Vec<u8>,
    lines: Vec<(u64, Vec<u8>)>,
}

struct Capture {
    warn: bool,
    limits: Limits,
    title: Vec<u8>,
    body: Vec<u8>,
    commits: Vec<(String, Vec<u8>)>,
    files: Vec<CapFile>,
}

/// Why a capture could not be read. Constant text only: nothing from the
/// capture reaches the message.
type LoadError = &'static str;

/// Reads the AGCAP1 format of bench/capture.py. Every malformed input is an
/// Err, never a panic or a partial capture.
fn load(data: &[u8]) -> Result<Capture, LoadError> {
    const MALFORMED: LoadError = "the capture is malformed";
    let magic = b"AGCAP1\n";
    if !data.starts_with(magic) {
        return Err("the capture is not an AGCAP1 capture");
    }
    let mut pos = magic.len();
    let mut cap = Capture {
        warn: false,
        limits: Limits::default(),
        title: Vec::new(),
        body: Vec::new(),
        commits: Vec::new(),
        files: Vec::new(),
    };
    fn number<T: std::str::FromStr>(text: Option<&&str>) -> Result<T, LoadError> {
        text.ok_or(MALFORMED)?.parse().map_err(|_| MALFORMED)
    }
    loop {
        let end = pos + data.get(pos..).ok_or(MALFORMED)?.iter().position(|&b| b == b'\n').ok_or(MALFORMED)?;
        let header = std::str::from_utf8(&data[pos..end]).map_err(|_| MALFORMED)?;
        pos = end + 1;
        let parts: Vec<&str> = header.split(' ').collect();
        let mut payload = |size: Option<&&str>| -> Result<Vec<u8>, LoadError> {
            let size: usize = number(size)?;
            let stop = pos.checked_add(size).ok_or(MALFORMED)?;
            if data.get(stop) != Some(&b'\n') {
                return Err("the capture is truncated");
            }
            let chunk = data[pos..stop].to_vec();
            pos = stop + 1;
            Ok(chunk)
        };
        match parts[0] {
            "end" => {
                return if pos == data.len() { Ok(cap) } else { Err(MALFORMED) };
            }
            "mode" => cap.warn = parts.get(1) == Some(&"warn"),
            "limit" => {
                let value: u64 = number(parts.get(2))?;
                match parts.get(1).copied() {
                    Some("work") => cap.limits.work = value,
                    Some("findings") => cap.limits.findings = value,
                    Some("line_length") => cap.limits.line_length = value,
                    Some("hit") => cap.limits.hit = value,
                    _ => return Err(MALFORMED),
                }
            }
            "title" => cap.title = payload(parts.get(1))?,
            "body" => cap.body = payload(parts.get(1))?,
            "commit" => {
                let sha = parts.get(1).ok_or(MALFORMED)?.to_string();
                let message = payload(parts.get(2))?;
                cap.commits.push((sha, message));
            }
            "file" => {
                let kind = parts.get(1).ok_or(MALFORMED)?.to_string();
                let path = payload(parts.get(2))?;
                cap.files.push(CapFile { kind, path, lines: Vec::new() });
            }
            "line" => {
                let number: u64 = number(parts.get(1))?;
                let raw = payload(parts.get(2))?;
                cap.files.last_mut().ok_or(MALFORMED)?.lines.push((number, raw));
            }
            _ => return Err(MALFORMED),
        }
    }
}

/// gitdata._classify, for a new file whose section is already known.
fn classify(file: &CapFile) -> Changed {
    let path = match std::str::from_utf8(&file.path) {
        Ok(path) => path.to_string(),
        Err(_) => {
            return Changed {
                path: String::from_utf8_lossy(&file.path).into_owned(),
                kind: Kind::Undecodable,
                added: Vec::new(),
            }
        }
    };
    if file.kind == "submodule" {
        return Changed { path, kind: Kind::Submodule, added: Vec::new() };
    }
    if file.kind == "binary" {
        let ext = extension(&path).to_lowercase();
        let kind = if BINARY_EXTENSIONS.contains(&ext.as_str()) { Kind::AllowedBinary } else { Kind::RejectedBinary };
        return Changed { path, kind, added: Vec::new() };
    }
    let mut added = Vec::with_capacity(file.lines.len());
    for (number, raw) in &file.lines {
        match std::str::from_utf8(raw) {
            Ok(text) => added.push((*number, text.trim_end_matches('\r').to_string())),
            Err(_) => return Changed { path, kind: Kind::Undecodable, added: Vec::new() },
        }
    }
    Changed { path, kind: Kind::Text, added }
}

// report.py
fn render(text: &str) -> String {
    let shown: Vec<char> = visible_str(text).chars().collect();
    let mut out = String::with_capacity(shown.len());
    for (index, &c) in shown.iter().enumerate() {
        if c == '#' && shown.get(index + 1) == Some(&'#') && shown.get(index + 2) == Some(&'[') {
            out.push_str("<U+0023>");
        } else {
            out.push(c);
        }
    }
    out
}

fn escape_data(text: &str, property: bool) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '%' => out.push_str("%25"),
            '\r' => out.push_str("%0D"),
            '\n' => out.push_str("%0A"),
            ':' if property => out.push_str("%3A"),
            ',' if property => out.push_str("%2C"),
            _ => out.push(c),
        }
    }
    out
}

/// report.Reporter: every line is written and flushed on its own, so a
/// reader sees each finding as soon as it is reported, as with the Python
/// action. The first failed write stops the report.
struct Reporter<W: std::io::Write> {
    out: W,
}

impl<W: std::io::Write> Reporter<W> {
    fn write(&mut self, line: &str) -> std::io::Result<()> {
        self.out.write_all(line.as_bytes())?;
        self.out.write_all(b"\n")?;
        self.out.flush()
    }
    fn log(&mut self, text: &str) -> std::io::Result<()> {
        let line = format!("agent-guardrails: {}", render(text));
        self.write(&line)
    }
    fn annotate(
        &mut self,
        severity: &str,
        title: &str,
        message: &str,
        file: Option<&str>,
        line: Option<u64>,
    ) -> std::io::Result<()> {
        let mut properties = Vec::new();
        if let Some(file) = file {
            properties.push(format!("file={}", escape_data(&render(file), true)));
            if let Some(line) = line {
                properties.push(format!("line={line}"));
            }
        }
        properties.push(format!("title={}", escape_data(&render(title), true)));
        let command = format!("::{severity} {}::{}", properties.join(","), escape_data(&render(message), false));
        self.write(&command)?;
        self.log(&format!("{severity}: {title}: {message}"))
    }
    fn finding(&mut self, finding: &Finding) -> std::io::Result<()> {
        self.annotate(finding.severity, finding.title, &finding.message, finding.file.as_deref(), finding.line)
    }
}

fn plural(n: usize) -> &'static str {
    if n == 1 { "" } else { "s" }
}

/// main._check from settings.load onwards, for the hidden Unicode check
/// only, reporting in the same order as the Python drive: the "not scanned"
/// lines as each file is classified, then every finding, then the summary.
fn drive<W: std::io::Write>(cap: &Capture, out: &mut Reporter<W>) -> std::io::Result<u8> {
    let ctx = Ctx { limits: &cap.limits, warn: cap.warn };
    let mut budget = Budget::default();
    let mut findings = Vec::new();
    let checked = (|| -> Result<std::io::Result<()>, LimitReached> {
        let title = String::from_utf8_lossy(&cap.title);
        let body = String::from_utf8_lossy(&cap.body);
        findings.append(&mut check_unicode(&ctx, &title, "the PR title", &mut budget)?);
        findings.append(&mut check_unicode(&ctx, &body, "the PR body", &mut budget)?);
        for (sha, message) in &cap.commits {
            let message = String::from_utf8_lossy(message);
            findings.append(&mut check_unicode(&ctx, &message, &format!("the message of commit {sha}"), &mut budget)?);
        }
        for file in &cap.files {
            let changed = classify(file);
            let logged = if changed.kind == Kind::AllowedBinary {
                out.log(&format!("not scanned (binary): {}", changed.path))
            } else if changed.kind == Kind::Submodule {
                out.log(&format!("not scanned (submodule): {}", changed.path))
            } else {
                Ok(())
            };
            if logged.is_err() {
                return Ok(logged);
            }
            findings.append(&mut check_changed_file(&ctx, &changed, &mut budget)?);
        }
        Ok(Ok(()))
    })();
    match checked {
        Err(error) => {
            for finding in findings.iter().chain(error.findings.iter()) {
                out.finding(finding)?;
            }
            out.annotate("error", "agent-guardrails", &error.message, None, None)?;
            Ok(1)
        }
        Ok(written) => {
            written?;
            for finding in &findings {
                out.finding(finding)?;
            }
            let errors = findings.iter().filter(|f| f.severity == "error").count();
            let warnings = findings.len() - errors;
            let (commits, files) = (cap.commits.len(), cap.files.len());
            out.log(&format!(
                "checked {commits} commit{}, {files} changed file{}, and the PR title and body: {errors} error{}, {warnings} warning{}",
                plural(commits),
                plural(files),
                plural(errors),
                plural(warnings)
            ))?;
            Ok(u8::from(errors > 0))
        }
    }
}

/// One constant line on stderr. Nothing from the capture, the environment or
/// a panic payload is ever printed here.
fn fail(message: &str) -> ExitCode {
    let _ = writeln!(std::io::stderr(), "agent-guardrails: {message}");
    ExitCode::from(1)
}

fn read_capture(path: &std::ffi::OsStr) -> Result<Vec<u8>, LoadError> {
    let file = std::fs::File::open(path).map_err(|_| "cannot open the capture")?;
    let mut data = Vec::new();
    file.take(MAX_CAPTURE_BYTES + 1).read_to_end(&mut data).map_err(|_| "cannot read the capture")?;
    if data.len() as u64 > MAX_CAPTURE_BYTES {
        return Err("the capture is larger than the 64 MiB this experiment reads");
    }
    Ok(data)
}

fn run() -> ExitCode {
    let args: Vec<std::ffi::OsString> = std::env::args_os().collect();
    if args.len() == 2 && args[1] == "--unidata" {
        println!("{}", tables::UNIDATA_VERSION);
        return ExitCode::SUCCESS;
    }
    if args.len() != 3 || args[1] != "drive" {
        return fail("usage: agent-guardrails drive <capture>");
    }
    let cap = match read_capture(&args[2]).and_then(|data| load(&data)) {
        Ok(cap) => cap,
        Err(message) => return fail(message),
    };
    let stdout = std::io::stdout();
    let mut out = Reporter { out: stdout.lock() };
    match drive(&cap, &mut out) {
        Ok(code) => ExitCode::from(code),
        Err(_) => fail("cannot write the report"),
    }
}

fn main() -> ExitCode {
    // A panic prints this constant line and nothing else: never the payload,
    // which could quote the capture, and never the environment.
    std::panic::set_hook(Box::new(|_| {
        let _ = writeln!(std::io::stderr(), "agent-guardrails: internal error");
    }));
    std::panic::catch_unwind(run).unwrap_or(ExitCode::from(1))
}
