//! Typed 2D CAD intermediate representation (the IR of ``cad2d-ir``) and a DXF
//! writer for it. Experimental: the Python package stays the reference, and the
//! writer here is checked against it byte for byte.

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
