//! Typed 2D CAD intermediate representation (the IR of ``cad2d-ir``) and the
//! DXF writer of ``cad2d-ir``: the Python package hands its documents to it.
//! The writer replaced one written in Python (up to cad2d-ir 0.17.22) and
//! writes the same text; the golden files under ``native/tests`` keep it so.

pub mod convert;
pub mod dxf;
pub mod model;
pub mod num;
pub mod pystr;
pub mod value;

pub use model::{
    Block, Color, Document, EllipseData, Entity, HatchData, HatchLoop, InsertData, InsertScale,
    Interner, Point2, PolylineData, Shape, Tables, TextData, Vertex,
};
pub use value::{Map, Value};
