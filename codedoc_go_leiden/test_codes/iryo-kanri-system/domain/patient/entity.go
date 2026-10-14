package patient

import "fmt"

type Entity struct {
	ID int
}

func (e Entity) Describe() string {
	return fmt.Sprintf("エンティティ#%d", e.ID)
}

func (e *Entity) Touch() int {
	e.ID++
	return e.ID
}

func (e Entity) Save() string {
	return fmt.Sprintf("保存#%d", e.ID)
}

type AuditedEntity struct {
	Entity
	Audit []string
}

func (a *AuditedEntity) Record(note string) int {
	a.Audit = append(a.Audit, note)
	return len(a.Audit)
}

type Patient struct {
	AuditedEntity
	Name string
}

func (p Patient) Describe() string {
	return "患者 " + p.Name
}

func (p Patient) Save() string {
	return "患者保存 " + p.Name + " / " + p.Entity.Save()
}

type Inpatient struct {
	Patient
	Ward string
}
