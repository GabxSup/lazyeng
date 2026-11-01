#!/usr/bin/env perl
# compress.sh — shrink natural-language prompts before sending them to a model.
#
#   echo "Please write me a function that takes a list and returns only the unique items" | ./compress.sh
#   ./compress.sh "inline prompt text"
#   ./compress.sh < prompt.txt
#
# Rewrites only prose. Fenced code blocks and inline `code` are passed through
# byte-for-byte, so prompts that contain examples survive.
#
# This is lossy by design: read the output before you send it.
#
# Requires: perl 5.10+ (preinstalled on macOS and virtually every Linux).

use strict;
use warnings;

my $VERSION = '2.0.0';

my $usage = <<'END';
Usage: compress.sh [OPTIONS] [PROMPT...]

Shrinks a natural-language prompt: strips politeness and filler, collapses
"write me a function that takes X and returns Y" into "fn: X → Y", and tidies
whitespace. Fenced code blocks and inline `code` are never modified.

Arguments:
  PROMPT...      prompt text as arguments (joined with spaces)
  -              read the prompt from stdin (default when no arguments)

Options:
  -h, --help     show this help
  -V, --version  show version
  --stats        also print before/after character counts to stderr

Examples:
  ./compress.sh "Could you please write a function that takes a list of users and returns the active ones"
  echo "In order to fix this, I would like you to just remove the extra spaces" | ./compress.sh

Exit codes: 0 ok · 2 bad usage
END

my $want_stats = 0;
my $literal    = 0;
my @args;
for my $a (@ARGV) {
    if ($literal)                           { push @args, $a }
    elsif ($a eq '--')                      { $literal = 1 }
    elsif ($a eq '-h' || $a eq '--help')    { print $usage; exit 0 }
    elsif ($a eq '-V' || $a eq '--version') { print "compress.sh $VERSION\n"; exit 0 }
    elsif ($a eq '--stats')                 { $want_stats = 1 }
    elsif ($a eq '-')                       { next }
    else                                    { push @args, $a }
}

my $input = @args ? join(' ', @args) : do { local $/; <STDIN> };
$input = '' unless defined $input;

my $had_newline = ($input =~ /\n\z/);
chomp $input;

my $out = compress_document($input);

print $out, "\n" if length $out;

if ($want_stats) {
    my $before = length $input;
    my $after  = length $out;
    my $pct    = $before ? int(100 - ($after * 100 / $before)) : 0;
    printf STDERR "chars: %d -> %d (%d%% smaller)\n", $before, $after, $pct;
}

exit 0;

# ── document level ───────────────────────────────────────────────────────────
# Splits on fenced code blocks so only prose is rewritten.
sub compress_document {
    my ($text) = @_;
    return '' unless length $text;

    my @lines   = split /\n/, $text, -1;
    my @out;
    my $in_fence = 0;
    my $fence    = '';

    for my $line (@lines) {
        if ($line =~ /^[ \t]*(`{3,}|~{3,})/) {
            my $marker = $1;
            if (!$in_fence) {
                $in_fence = 1;
                $fence    = $marker;
            }
            elsif (substr($marker, 0, 1) eq substr($fence, 0, 1)
                && length($marker) >= length($fence)) {
                $in_fence = 0;
            }
            push @out, $line;
            next;
        }
        push @out, $in_fence ? $line : compress_line($line);
    }

    my $result = join "\n", @out;
    $result =~ s/\n{3,}/\n\n/g;    # collapse runs of blank lines left behind
    $result =~ s/\A\s+//;
    $result =~ s/\s+\z//;
    return $result;
}

# ── line level ───────────────────────────────────────────────────────────────
sub compress_line {
    my ($line) = @_;

    # Headings, tables, quotes and rules are structure: rewrite them and the
    # Markdown breaks. Blank lines likewise.
    return $line if $line =~ /\A\s*\z/;
    return $line if $line =~ /\A\s*(?:#|\||>|-{3,}|\*{3,}|=)/;

    my ($indent) = $line =~ /\A(\s*)/;
    (my $body = $line) =~ s/\A\s+//;

    # Bullets and numbered items keep their marker; only the text is compressed.
    my $marker = '';
    if ($body =~ /\A((?:[-*+]|\d+[.)])\s+)(.*)\z/s) {
        $marker = $1;
        $body   = $2;
    }

    # Mask inline code so `just`, `really` etc. inside identifiers survive.
    my @code;
    $body =~ s/(`+[^`]*`+)/push(@code, $1), "\x01" . $#code . "\x01"/ge;

    # 1. Leading politeness, repeatedly ("Please can you just...").
    for (1 .. 3) {
        last unless $body =~ s/\A(?:hi|hello|hey)(?:\s+there)?[,!\s]+//i
                          or $body =~ s/\A(?:please|pls|kindly)[,!\s]+//i
                          or $body =~ s/\A(?:there|thanks|thank\s+you)[,!\s]+//i
                          or $body =~ s/\A(?:could|can|would|will)\s+you\s+(?:please\s+)?//i
                          or $body =~ s/\A(?:i\s+would\s+like|i\s+need|i\s+want)\s+you\s+to\s+//i
                          or $body =~ s/\Ahelp\s+me\s+(?:to\s+)?//i;
    }

    # 2. Politeness and filler anywhere in the sentence.
    $body =~ s/\bin\s+order\s+to\b/to/gi;
    $body =~ s/\b(?:please|basically|actually|simply|obviously|clearly|really|just)\b[ \t]*//gi;
    $body =~ s/\b(?:i\s+would\s+like|i\s+need|i\s+want)\s+you\s+to\s+//gi;
    $body =~ s/\b(?:could|can|would|will)\s+you\s+(?:please\s+)?//gi;
    $body =~ s/\bhelp\s+me\s+(?:to\s+)?//gi;

    # 3. "write me a function that takes X and returns Y" -> "fn: X → Y"
    $body =~ s/\b(?:write|create|build|make|give\s+me)\s+(?:me\s+)?(?:a|an|the)\s+
              (?:function|method|helper|routine|script|program|snippet|one-?liner)
              \s+(?:that|which|to)\s+/fn: /gix;

    # 4. "create a React component that displays X" -> "React: X"
    # Acronyms keep their type word ("API endpoint: ..."), product names don't
    # ("React: ..."), because "API: auth" is ambiguous and "React: users" isn't.
    # An interpolated qr// is wrapped in (?^:...), which resets flags — so each
    # fragment carries its own /i instead of inheriting from the s/// flags.
    my $make = qr/\b(?:create|build|make|write|add)\s+(?:me\s+)?(?:a|an|the)\s+/i;
    my $noun = qr/component|module|class|service|hook|endpoint|controller|provider/i;
    my $verb = qr/(?:displays?|shows?|renders?|handles?|lists?|returns?|fetches?|loads?)/i;
    $body =~ s{$make((?-i:[A-Z0-9]{2,}))\s+($noun)\s*(?:that|which)\s+$verb\s+}
              {${1} ${2}: }gix;
    $body =~ s{$make(\S+)\s+(?:$noun)\s*(?:that|which)\s+$verb\s+}{${1}: }gix;

    # 5. Signature compression: drop "takes/accepts a list of", then "takes a".
    $body =~ s/\b(?:takes?|accepts?|receives?)\s+(?:a|an|the)\s+
              (?:list|array|collection|sequence|slice|set)\s+of\s+//gix;
    $body =~ s/\b(?:that|which)\s+(?:takes?|accepts?)\s+(?:a|an|the)?\s*//gix;
    $body =~ s/\b(?:takes?|accepts?)\s+(?:a|an|the)\s+//gix;

    # 6. "and returns X" -> "→ X"
    $body =~ s/[ \t]+(?:and|that|which)\s+returns?\s+/ → /gi;

    # 7. Inside an already-compressed signature, drop a leading "a list of".
    $body =~ s/\A(\w+:\s*)(?:a|an|the)?\s*
              (?:list|array|collection|sequence)\s+of\s+/$1/gix;

    # 8. Whitespace and punctuation tidy-up.
    $body =~ s/[ \t]{2,}/ /g;
    $body =~ s/\s+\z//;
    $body =~ s/\s+([,.;:!?])/$1/g;
    $body =~ s/\s+→\s*\z//;          # dangling arrow
    $body =~ s/\Afn\s*:\s*\z//;      # signature with nothing in it
    $body =~ s/\A[:;,\-–\s]+//;

    # Restore masked inline code.
    $body =~ s/\x01(\d+)\x01/$code[$1]/g;

    return $body eq '' ? '' : $indent . $marker . $body;
}
