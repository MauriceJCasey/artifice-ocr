# Artifice region vocabulary in PAGE XML

Artifice writes PRImA PAGE XML using the 2019-07-15 namespace. Annotation
semantics that are finer than PAGE's legal `TextRegion/@type` enumeration are
also recorded in `TextRegion/@custom` using Transkribus-compatible syntax:

```text
structure {type:marginalia;}
```

The supported Artifice values are:

| Artifice value | PAGE `TextRegion/@type` |
| --- | --- |
| `marginalia` | `marginalia` |
| `interlinear` | `other` |
| `underline` | `other` |
| `strikethrough` | `other` |
| `circling` | `other` |
| `symbol` | `other` |
| `unclassified` | `other` |

Unknown provider labels must be converted to `unclassified`; they must not be
dropped. Provider-specific label maps should be explicit and documented next
to their implementation.

Artifice uses `TextEquiv/@index` consistently: `0` is raw OCR, `1` is cleaned
text, and `2` is translation. A correction replaces the value at the index
being reviewed; it does not create another index. Titles are not PAGE metadata.
